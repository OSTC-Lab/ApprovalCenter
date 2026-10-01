import asyncio
import logging
from typing import Protocol

from approval_center.approval import ApprovalService, MessageSnapshot, MessageSyncResult
from approval_center.config import Config

LOGGER = logging.getLogger(__name__)


class MessagePublisher(Protocol):
	@property
	def ready(self) -> bool: ...

	async def sync(self, snapshot: MessageSnapshot, display_name: str) -> MessageSyncResult: ...


class Maintenance:
	def __init__(self, service: ApprovalService, publisher: MessagePublisher, config: Config):
		self.service = service
		self.publisher = publisher
		self.config = config
		self.healthy = False
		self._sync_failed = False

	async def run_once(self) -> None:
		await self.service.expire_due()
		await self.service.cleanup()
		success = True
		if self.publisher.ready:
			for snapshot in await self.service.pending_messages():
				client = self.config.find_client(snapshot.approval.client_id)
				display_name = client.display_name if client is not None else snapshot.approval.client_id
				try:
					result = await self.publisher.sync(snapshot, display_name)
					await self.service.complete_message_sync(snapshot, result.message_id, result.missing)
					LOGGER.info('Approval message synchronized approval_id=%s message_id=%s', snapshot.approval.approval_id, result.message_id)
				except Exception:
					success = False
					LOGGER.exception('Approval message synchronization failed approval_id=%s', snapshot.approval.approval_id)
		if self._sync_failed and success and self.publisher.ready:
			LOGGER.info('Approval message synchronization recovered')
		self._sync_failed = not success
		self.healthy = success

	async def run(self) -> None:
		try:
			while True:
				try:
					await self.run_once()
				except Exception:
					self.healthy = False
					LOGGER.exception('Maintenance cycle failed')
				await asyncio.sleep(self.config.policy.maintenance_interval)
		finally:
			self.healthy = False
