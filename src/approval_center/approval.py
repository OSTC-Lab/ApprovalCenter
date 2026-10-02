import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from approval_center.config import ClientConfig, PolicyConfig, Snowflake

if TYPE_CHECKING:
	from approval_center.storage import Storage, Transaction


LOGGER = logging.getLogger(__name__)
MAX_CONTENT_CHARACTERS = 5500  # Reserve 500 embed characters for center metadata.


class ApprovalStatus(StrEnum):
	PENDING = 'pending'
	APPROVED = 'approved'
	REJECTED = 'rejected'
	TIMED_OUT = 'timed_out'
	CANCELLED = 'cancelled'


class StoredModel(BaseModel):
	model_config = ConfigDict(extra='forbid', frozen=True)


class DisplayField(StoredModel):
	name: str = Field(min_length=1, max_length=256)
	value: str = Field(min_length=1, max_length=1024)
	inline: bool = False


class ApprovalContent(StoredModel):
	title: str = Field(min_length=1, max_length=256)
	description: str = Field(default='', max_length=4096)
	fields: tuple[DisplayField, ...] = Field(default=(), max_length=25)

	@model_validator(mode='after')
	def validate_size(self) -> 'ApprovalContent':
		text = [self.title, self.description]
		text.extend(part for field in self.fields for part in (field.name, field.value))
		# UTF-16 counting also covers astral characters without underestimating capacity.
		for part, limit in ((self.title, 256), (self.description, 4096)):
			if len(part.encode('utf-16-le')) // 2 > limit:
				raise ValueError('Display title or description exceeds the single-card capacity')
		for field in self.fields:
			if len(field.name.encode('utf-16-le')) // 2 > 256 or len(field.value.encode('utf-16-le')) // 2 > 1024:
				raise ValueError('Display field exceeds the single-card capacity')
		if sum(len(part.encode('utf-16-le')) // 2 for part in text) > MAX_CONTENT_CHARACTERS:
			raise ValueError('Display content exceeds the single-card capacity')
		if not self.title.strip() or any(not field.name.strip() or not field.value.strip() for field in self.fields):
			raise ValueError('Display title, field names and values must not be blank')
		return self


class Approval(StoredModel):
	approval_id: int
	client_id: str
	reference_key: str | None
	content: ApprovalContent
	status: ApprovalStatus
	created_at: int
	expires_at: int
	updated_at: int
	reviewer_name: str | None
	decided_at: int | None
	data: bytes
	data_version: int


class MessageLink(StoredModel):
	approval_id: int
	guild_id: Snowflake
	channel_id: Snowflake
	message_id: Snowflake | None = None
	needs_message_sync: bool = True


@dataclass(frozen=True)
class ApprovalFilter:
	status: ApprovalStatus | None = None
	reference_key: str | None = None
	created_from: int | None = None
	created_before: int | None = None
	updated_from: int | None = None
	updated_before: int | None = None
	limit: int = 100
	offset: int = 0
	all_clients: bool = False


@dataclass(frozen=True)
class MessageSnapshot:
	approval: Approval
	link: MessageLink


@dataclass(frozen=True)
class DecisionResult:
	approval: Approval
	accepted: bool


@dataclass(frozen=True)
class MessageSyncResult:
	message_id: str
	missing: bool = False


class ApprovalError(Exception):
	def __init__(self, code: str, message: str, status_code: int):
		super().__init__(message)
		self.code = code
		self.message = message
		self.status_code = status_code


class ApprovalService:
	def __init__(self, storage: 'Storage', policy: PolicyConfig, guild_id: str, channel_id: str, clock: Callable[[], float] = time.time):
		self.storage = storage
		self.policy = policy
		self.guild_id = guild_id
		self.channel_id = channel_id
		self.clock = clock

	def now(self) -> int:
		return int(self.clock())

	def _check_data(self, data: bytes) -> None:
		if len(data) > self.policy.max_data_bytes:
			raise ApprovalError('invalid_request', 'Custom data exceeds the configured size limit', 422)

	async def create(self, client: ClientConfig, content: ApprovalContent, expires_at: int, data: bytes = b'', *, reference_key: str | None = None) -> Approval:
		self._check_data(data)
		now = self.now()
		if not now < expires_at <= now + self.policy.max_approval_seconds:
			raise ApprovalError('invalid_request', 'Approval deadline must be in the future and within the configured limit', 422)
		async with self.storage.transaction() as transaction:
			approval = await transaction.create(client.client_id, content, expires_at, data, self.guild_id, self.channel_id, now, reference_key=reference_key)
		LOGGER.info('Approval created approval_id=%s client_id=%s', approval.approval_id, client.client_id)
		return approval

	async def _expire(self, transaction: 'Transaction', approval: Approval, now: int) -> Approval:
		if approval.status == ApprovalStatus.PENDING and now >= approval.expires_at:
			await transaction.set_decision(approval.approval_id, ApprovalStatus.TIMED_OUT, None, approval.expires_at, now)
			LOGGER.info('Approval timed out approval_id=%s client_id=%s', approval.approval_id, approval.client_id)
			return approval.model_copy(update={'status': ApprovalStatus.TIMED_OUT, 'reviewer_name': None, 'decided_at': approval.expires_at, 'updated_at': now})
		return approval

	async def _get(self, transaction: 'Transaction', approval_id: int, client: ClientConfig | None, now: int) -> Approval:
		approval = await transaction.get(approval_id)
		if approval is None or (client is not None and not client.is_admin and approval.client_id != client.client_id):
			raise ApprovalError('approval_not_found', 'Approval not found', 404)
		if now >= approval.expires_at + self.policy.retention_seconds:
			raise ApprovalError('approval_not_found', 'Approval not found', 404)
		return await self._expire(transaction, approval, now)

	async def get(self, client: ClientConfig, approval_id: int) -> Approval:
		async with self.storage.transaction() as transaction:
			return await self._get(transaction, approval_id, client, self.now())

	async def cancel(self, client: ClientConfig, approval_id: int) -> Approval:
		async with self.storage.transaction() as transaction:
			approval = await self._get(transaction, approval_id, client, self.now())
			now = self.now()
			approval = await self._expire(transaction, approval, now)
			changed = approval.status == ApprovalStatus.PENDING
			if changed:
				await transaction.set_decision(approval_id, ApprovalStatus.CANCELLED, None, now, now)
				updated = await transaction.get(approval_id)
				assert updated is not None
				approval = updated
		# Commit any timeout before reporting that cancellation is no longer possible.
		if approval.status != ApprovalStatus.CANCELLED:
			raise ApprovalError('approval_not_pending', 'Only pending approvals may be cancelled', 409)
		if changed:
			LOGGER.info('Approval cancelled approval_id=%s actor_client_id=%s owner_client_id=%s', approval_id, client.client_id, approval.client_id)
		return approval

	async def set_status(self, client: ClientConfig, approval_id: int, status: ApprovalStatus) -> Approval:
		if not client.is_admin:
			raise ApprovalError('forbidden', 'Only administrator clients may set approval status', 403)
		conflict: str | None = None
		async with self.storage.transaction() as transaction:
			approval = await self._get(transaction, approval_id, client, self.now())
			now = self.now()
			approval = await self._expire(transaction, approval, now)
			previous_status = approval.status
			if status == ApprovalStatus.PENDING and now >= approval.expires_at:
				conflict = 'Expired approvals cannot be restored to pending'
			elif status == ApprovalStatus.TIMED_OUT and now < approval.expires_at:
				conflict = 'Approval deadline has not been reached'
			elif status != previous_status:
				if status == ApprovalStatus.PENDING:
					reviewer_name, decided_at = None, None
				elif status == ApprovalStatus.TIMED_OUT:
					reviewer_name, decided_at = None, approval.expires_at
				else:
					reviewer_name, decided_at = client.display_name, now
				await transaction.set_decision(approval_id, status, reviewer_name, decided_at, now)
				updated = await transaction.get(approval_id)
				assert updated is not None
				approval = updated
		# Keep any automatic timeout even when the requested transition is invalid.
		if conflict is not None:
			raise ApprovalError('invalid_status_transition', conflict, 409)
		if status != previous_status:
			LOGGER.info(
				'Approval status changed approval_id=%s actor_client_id=%s owner_client_id=%s previous_status=%s status=%s',
				approval_id, client.client_id, approval.client_id, previous_status.value, status.value,
			)
		return approval

	async def list_approvals(self, client: ClientConfig, filters: ApprovalFilter) -> list[Approval]:
		if filters.all_clients and not client.is_admin:
			raise ApprovalError('forbidden', 'Only administrator clients may list all approvals', 403)
		for start, end in ((filters.created_from, filters.created_before), (filters.updated_from, filters.updated_before)):
			if start is not None and end is not None and start >= end:
				raise ApprovalError('invalid_request', 'Time range start must be earlier than its end', 422)
		async with self.storage.transaction() as transaction:
			now = self.now()
			owner = None if filters.all_clients else client.client_id
			await transaction.expire_due(now, owner)
			return await transaction.list_approvals(owner, filters, now - self.policy.retention_seconds)

	async def replace_data(self, client: ClientConfig, approval_id: int, data: bytes, expected_version: int | None) -> Approval:
		self._check_data(data)
		async with self.storage.transaction() as transaction:
			now = self.now()
			approval = await self._get(transaction, approval_id, client, now)
			if expected_version is not None and expected_version != approval.data_version:
				raise ApprovalError('data_version_conflict', 'Custom data version does not match', 409)
			await transaction.replace_data(approval_id, data, approval.data_version + 1, now)
			approval = approval.model_copy(update={'data': data, 'data_version': approval.data_version + 1, 'updated_at': now})
		LOGGER.info('Custom data replaced approval_id=%s actor_client_id=%s owner_client_id=%s version=%s', approval_id, client.client_id, approval.client_id, approval.data_version)
		return approval

	async def decide(self, approval_id: int, status: ApprovalStatus, reviewer_name: str, guild_id: str, channel_id: str, message_id: str) -> DecisionResult:
		if status not in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
			raise ValueError('Decision must be approved or rejected')
		async with self.storage.transaction() as transaction:
			now = self.now()
			approval = await self._get(transaction, approval_id, None, now)
			link = await transaction.get_link(approval_id)
			if link is None or (link.guild_id, link.channel_id, link.message_id) != (guild_id, channel_id, message_id):
				raise ApprovalError('invalid_message', 'This message is not associated with the approval', 403)
			now = self.now()
			approval = await self._expire(transaction, approval, now)
			if approval.status != ApprovalStatus.PENDING:
				return DecisionResult(approval, False)
			await transaction.set_decision(approval_id, status, reviewer_name, now, now)
			approval = approval.model_copy(update={'status': status, 'reviewer_name': reviewer_name, 'decided_at': now, 'updated_at': now})
		LOGGER.info('Approval decided approval_id=%s client_id=%s reviewer_name=%s status=%s', approval_id, approval.client_id, reviewer_name, status.value)
		return DecisionResult(approval, True)

	async def expire_due(self) -> int:
		async with self.storage.transaction() as transaction:
			count = await transaction.expire_due(self.now())
		if count:
			LOGGER.info('Approvals timed out count=%s', count)
		return count

	async def pending_messages(self) -> list[MessageSnapshot]:
		async with self.storage.transaction() as transaction:
			return await transaction.pending_messages(self.now() - self.policy.retention_seconds)

	async def complete_message_sync(self, snapshot: MessageSnapshot, message_id: str, missing: bool = False) -> None:
		async with self.storage.transaction() as transaction:
			approval = await transaction.get(snapshot.approval.approval_id)
			if approval is not None:
				needs_sync = not missing and (
					approval.status != snapshot.approval.status
					or approval.reviewer_name != snapshot.approval.reviewer_name
					or approval.decided_at != snapshot.approval.decided_at
				)
				await transaction.save_message(approval.approval_id, message_id, needs_sync)

	async def cleanup(self) -> int:
		async with self.storage.transaction() as transaction:
			count = await transaction.cleanup(self.now() - self.policy.retention_seconds)
		if count:
			LOGGER.info('Approvals cleaned count=%s', count)
		return count
