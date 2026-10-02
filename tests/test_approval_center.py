import asyncio
import base64
import logging
import sqlite3
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable, override
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import httpx
import discord
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

from approval_center.api import RuntimeHealth, install_api
from approval_center.approval import ApprovalContent, ApprovalError, ApprovalFilter, ApprovalService, ApprovalStatus, DisplayField, MessageSnapshot, MessageSyncResult
from approval_center.config import ClientConfig, Config, DiscordConfig, PolicyConfig, ServiceConfig, load_config
from approval_center.discord import ApprovalBot, DecisionButton, card_view, render_card
from approval_center.maintenance import Maintenance
from approval_center.runtime import create_app
from approval_center.storage import Storage


@dataclass
class Clock:
	seconds: int = 1000

	def __call__(self) -> float:
		return float(self.seconds)


@dataclass
class FakePublisher:
	ready: bool = True
	fail: bool = False
	missing: bool = False
	snapshots: list[MessageSnapshot] = field(default_factory=list)
	during_sync: Callable[[], Awaitable[None]] | None = None
	closed: bool = False

	async def sync(self, snapshot: MessageSnapshot, display_name: str) -> MessageSyncResult:
		if self.fail:
			raise RuntimeError('Simulated Discord failure')
		self.snapshots.append(snapshot)
		if self.during_sync is not None:
			await self.during_sync()
		return MessageSyncResult(snapshot.link.message_id or str(100 + snapshot.approval.approval_id), self.missing)

	async def start(self, token: str) -> None:
		await asyncio.Future()

	async def close(self) -> None:
		self.closed = True


def make_client(client_id: str, *, is_admin: bool = False, enabled: bool = True) -> ClientConfig:
	return ClientConfig(client_id=client_id, client_secret=SecretStr('secret'), display_name=client_id, is_admin=is_admin, enabled=enabled)


class ApprovalFixture(unittest.IsolatedAsyncioTestCase):
	@override
	async def asyncSetUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.path = Path(self.temp.name) / 'center.sqlite3'
		self.storage = await Storage.open(self.path)
		self.clock = Clock()
		self.owner = make_client('owner')
		self.other = make_client('other')
		self.admin = make_client('admin', is_admin=True)
		self.config = Config(
			service=ServiceConfig(database=self.path),
			discord=DiscordConfig(token=SecretStr('token'), guild_id='1', channel_id='2', reviewer_ids=['3']),
			clients=[self.owner, self.other, self.admin, make_client('disabled', enabled=False)],
			policy=PolicyConfig(max_approval_seconds=1000, retention_seconds=20, max_data_bytes=8),
		)
		self.service = ApprovalService(self.storage, self.config.policy, '1', '2', self.clock)
		self.content = ApprovalContent(title='申请回档', description='理由', fields=(DisplayField(name='玩家', value='Steve'),))
		self.publisher = FakePublisher()
		self.maintenance = Maintenance(self.service, self.publisher, self.config)

	@override
	async def asyncTearDown(self) -> None:
		await self.storage.close()
		self.temp.cleanup()

	async def create(self, *, owner: ClientConfig | None = None, expires_at: int = 1100, reference_key: str | None = None):
		return await self.service.create(owner or self.owner, self.content, expires_at, b'\x00\xff', reference_key=reference_key)

	async def assert_error(self, expected_code: int, operation: Awaitable[object]) -> None:
		with self.assertRaises(ApprovalError) as caught:
			await operation
		self.assertEqual(caught.exception.status_code, expected_code)


class ApprovalTests(ApprovalFixture):
	async def test_storage_roundtrip_and_restart(self) -> None:
		approval = await self.create()
		self.assertEqual(approval.approval_id, 1)
		self.assertEqual(approval.data_version, 0)
		self.assertEqual(await self.service.get(self.owner, 1), approval)
		await self.storage.close()
		self.storage = await Storage.open(self.path)
		self.service.storage = self.storage
		self.assertEqual(await self.service.get(self.owner, 1), approval)
		self.assertEqual((await self.service.pending_messages())[0].link.guild_id, '1')

	async def test_create_atomicity_and_transaction_rollback(self) -> None:
		with self.assertRaises(sqlite3.IntegrityError):
			async with self.storage.transaction() as transaction:
				await transaction.create('owner', self.content, 1100, b'', '1', '2', 1000)
				await transaction.connection.execute('INSERT INTO approval_data (approval_id) VALUES (999)')
		self.assertEqual(await self.service.list_approvals(self.owner, ApprovalFilter()), [])
		self.assertTrue(await self.storage.healthy())

	async def test_cancelled_transaction_releases_storage(self) -> None:
		entered = asyncio.Event()

		async def interrupted() -> None:
			async with self.storage.transaction() as transaction:
				await transaction.create('owner', self.content, 1100, b'', '1', '2', 1000)
				entered.set()
				await asyncio.Future()

		task = asyncio.create_task(interrupted())
		await entered.wait()
		task.cancel()
		with self.assertRaises(asyncio.CancelledError):
			await task
		self.assertTrue(await self.storage.healthy())
		self.assertEqual(await self.service.list_approvals(self.owner, ApprovalFilter()), [])

	async def test_owner_and_admin_access(self) -> None:
		approval = await self.create()
		await self.create(owner=self.other)
		await self.assert_error(404, self.service.get(self.other, approval.approval_id))
		await self.assert_error(404, self.service.replace_data(self.other, approval.approval_id, b'x', None))
		await self.assert_error(403, self.service.list_approvals(self.owner, ApprovalFilter(all_clients=True)))
		self.assertEqual(await self.service.list_approvals(self.admin, ApprovalFilter()), [])
		self.assertEqual(len(await self.service.list_approvals(self.admin, ApprovalFilter(all_clients=True))), 2)
		updated = await self.service.replace_data(self.admin, approval.approval_id, b'admin', 0)
		self.assertEqual(updated.client_id, 'owner')
		self.assertEqual(updated.data, b'admin')

	async def test_list_order_ranges_and_pagination(self) -> None:
		first = await self.create()
		second = await self.create()
		self.clock.seconds += 1
		third = await self.create()
		items = await self.service.list_approvals(self.owner, ApprovalFilter(limit=1, offset=1))
		self.assertEqual([item.approval_id for item in items], [second.approval_id])
		items = await self.service.list_approvals(self.owner, ApprovalFilter(created_from=1000, created_before=1001))
		self.assertEqual([item.approval_id for item in items], [second.approval_id, first.approval_id])
		self.clock.seconds = 1002
		await self.service.replace_data(self.owner, first.approval_id, b'x', None)
		items = await self.service.list_approvals(self.owner, ApprovalFilter(updated_from=1002))
		self.assertEqual([item.approval_id for item in items], [first.approval_id])
		await self.assert_error(422, self.service.list_approvals(self.owner, ApprovalFilter(created_from=1001, created_before=1000)))
		self.assertEqual(third.created_at, 1001)

	async def test_data_versions_and_no_message_sync(self) -> None:
		approval = await self.create()
		await self.maintenance.run_once()
		self.clock.seconds = 1001
		updated = await self.service.replace_data(self.owner, approval.approval_id, b'first', 0)
		self.assertEqual(updated.data_version, 1)
		await self.assert_error(409, self.service.replace_data(self.owner, approval.approval_id, b'stale', 0))
		self.assertEqual((await self.service.get(self.owner, 1)).data, b'first')
		updated = await self.service.replace_data(self.owner, 1, b'force', None)
		self.assertEqual(updated.data_version, 2)
		self.assertEqual(updated.updated_at, 1001)
		self.assertEqual(updated.status, ApprovalStatus.PENDING)
		self.assertEqual(await self.service.pending_messages(), [])
		await self.assert_error(422, self.service.replace_data(self.owner, 1, b'012345678', None))
		self.assertEqual((await self.service.get(self.owner, 1)).data_version, 2)

	async def test_reference_key_scope_filters_and_persistence(self) -> None:
		key = 'survival/Steve'
		first = await self.create(reference_key=key, expires_at=1001)
		second = await self.create(reference_key=key)
		foreign = await self.create(owner=self.other, reference_key=key)
		await self.create(reference_key='survival/steve')
		await self.create(reference_key='survival/Steve/other')
		unkeyed = await self.create()
		self.assertIsNone(unkeyed.reference_key)
		self.assertEqual((await self.service.get(self.owner, second.approval_id)).reference_key, key)
		items = await self.service.list_approvals(self.owner, ApprovalFilter(reference_key=key))
		self.assertEqual([item.approval_id for item in items], [second.approval_id, first.approval_id])
		items = await self.service.list_approvals(self.owner, ApprovalFilter(reference_key=key, limit=1, offset=1))
		self.assertEqual([item.approval_id for item in items], [first.approval_id])
		self.assertEqual(len(await self.service.list_approvals(self.owner, ApprovalFilter())), 5)
		self.assertEqual(await self.service.list_approvals(self.admin, ApprovalFilter(reference_key=key)), [])
		items = await self.service.list_approvals(self.admin, ApprovalFilter(reference_key=key, all_clients=True))
		self.assertEqual([item.approval_id for item in items], [foreign.approval_id, second.approval_id, first.approval_id])
		await self.assert_error(403, self.service.list_approvals(self.owner, ApprovalFilter(reference_key=key, all_clients=True)))
		self.clock.seconds = 1001
		items = await self.service.list_approvals(self.owner, ApprovalFilter(reference_key=key, status=ApprovalStatus.PENDING))
		self.assertEqual([item.approval_id for item in items], [second.approval_id])
		items = await self.service.list_approvals(self.owner, ApprovalFilter(reference_key=key, updated_from=1001, updated_before=1002))
		self.assertEqual([item.approval_id for item in items], [first.approval_id])
		for literal in ('%', "' OR 1=1 --", '', '服务器/玩家'):
			with self.subTest(literal=literal):
				created = await self.create(reference_key=literal)
				items = await self.service.list_approvals(self.owner, ApprovalFilter(reference_key=literal))
				self.assertEqual([item.approval_id for item in items], [created.approval_id])
		updated = await self.service.replace_data(self.owner, second.approval_id, b'updated', None)
		self.assertEqual(updated.reference_key, key)
		await self.storage.close()
		self.storage = await Storage.open(self.path)
		self.service.storage = self.storage
		self.assertEqual((await self.service.get(self.owner, second.approval_id)).reference_key, key)

	async def test_concurrent_versioned_writes(self) -> None:
		await self.create()
		results = await asyncio.gather(
			self.service.replace_data(self.owner, 1, b'one', 0),
			self.service.replace_data(self.owner, 1, b'two', 0), return_exceptions=True,
		)
		self.assertEqual(sum(isinstance(result, ApprovalError) for result in results), 1)
		self.assertEqual((await self.service.get(self.owner, 1)).data_version, 1)

	async def test_concurrent_decisions_and_terminal_data(self) -> None:
		await self.create()
		await self.maintenance.run_once()
		results = await asyncio.gather(
			self.service.decide(1, ApprovalStatus.APPROVED, '3', '1', '2', '101'),
			self.service.decide(1, ApprovalStatus.REJECTED, '4', '1', '2', '101'),
		)
		self.assertEqual(sum(result.accepted for result in results), 1)
		approval = await self.service.get(self.owner, 1)
		self.assertEqual(approval.status, ApprovalStatus.APPROVED)
		self.clock.seconds = 1110
		updated = await self.service.replace_data(self.owner, 1, b'done', None)
		self.assertEqual(updated.status, ApprovalStatus.APPROVED)
		self.assertEqual(updated.reviewer_id, '3')
		self.assertEqual(updated.decided_at, 1000)
		self.assertEqual(updated.expires_at, 1100)

	async def test_message_binding(self) -> None:
		await self.create()
		await self.maintenance.run_once()
		for guild_id, channel_id, message_id in (('9', '2', '101'), ('1', '9', '101'), ('1', '2', '999')):
			await self.assert_error(403, self.service.decide(1, ApprovalStatus.APPROVED, '3', guild_id, channel_id, message_id))
		self.assertEqual((await self.service.get(self.owner, 1)).status, ApprovalStatus.PENDING)

	async def test_deadline_exact_boundary_and_list_filter(self) -> None:
		await self.create()
		await self.maintenance.run_once()
		self.clock.seconds = 1100
		result = await self.service.decide(1, ApprovalStatus.APPROVED, '3', '1', '2', '101')
		self.assertFalse(result.accepted)
		self.assertEqual(result.approval.status, ApprovalStatus.TIMED_OUT)
		self.assertEqual(result.approval.decided_at, 1100)
		self.assertIsNone(result.approval.reviewer_id)
		await self.create(expires_at=1101)
		self.clock.seconds = 1101
		self.assertEqual(await self.service.list_approvals(self.owner, ApprovalFilter(status=ApprovalStatus.PENDING)), [])
		self.assertEqual(len(await self.service.list_approvals(self.owner, ApprovalFilter(status=ApprovalStatus.TIMED_OUT))), 2)

	async def test_query_applies_timeout_before_maintenance(self) -> None:
		await self.create()
		self.clock.seconds = 1100
		approval = await self.service.get(self.owner, 1)
		self.assertEqual(approval.status, ApprovalStatus.TIMED_OUT)
		self.assertEqual((await self.service.pending_messages())[0].approval.status, ApprovalStatus.TIMED_OUT)

	async def test_expiry_and_data_limits(self) -> None:
		for deadline in (999, 1000, 2001):
			await self.assert_error(422, self.create(expires_at=deadline))
		await self.assert_error(422, self.service.create(self.owner, self.content, 1100, b'012345678'))
		self.assertEqual(await self.service.list_approvals(self.owner, ApprovalFilter()), [])

	async def test_sync_failure_recovery_and_restart(self) -> None:
		await self.create()
		self.publisher.fail = True
		with self.assertLogs('approval_center.maintenance', level=logging.ERROR):
			await self.maintenance.run_once()
		self.assertFalse(self.maintenance.healthy)
		self.assertEqual(len(await self.service.pending_messages()), 1)
		await self.storage.close()
		self.storage = await Storage.open(self.path)
		self.service.storage = self.storage
		self.publisher.fail = False
		await self.maintenance.run_once()
		self.assertTrue(self.maintenance.healthy)
		self.assertEqual(await self.service.pending_messages(), [])
		await self.service.decide(1, ApprovalStatus.REJECTED, '3', '1', '2', '101')
		self.publisher.fail = True
		with self.assertLogs('approval_center.maintenance', level=logging.ERROR):
			await self.maintenance.run_once()
		self.publisher.fail = False
		await self.maintenance.run_once()
		self.assertEqual(self.publisher.snapshots[-1].approval.status, ApprovalStatus.REJECTED)
		self.assertEqual(await self.service.pending_messages(), [])

	async def test_state_changes_during_publication(self) -> None:
		await self.create()

		async def expire_while_sending() -> None:
			self.clock.seconds = 1100
			await self.service.expire_due()

		self.publisher.during_sync = expire_while_sending
		await self.maintenance.run_once()
		snapshot = (await self.service.pending_messages())[0]
		self.assertEqual(snapshot.link.message_id, '101')
		self.assertEqual(snapshot.approval.status, ApprovalStatus.TIMED_OUT)
		self.publisher.during_sync = None
		await self.maintenance.run_once()
		self.assertEqual(await self.service.pending_messages(), [])
		self.assertEqual(self.publisher.snapshots[-1].approval.status, ApprovalStatus.TIMED_OUT)

	async def test_decision_rechecks_deadline_after_reading(self) -> None:
		await self.create()
		await self.maintenance.run_once()
		self.clock.seconds = 1099

		# A delayed storage lookup crosses the deadline inside an otherwise valid interaction.
		from approval_center.storage import Transaction
		get_link = Transaction.get_link

		async def delayed_link(transaction, approval_id):
			link = await get_link(transaction, approval_id)
			self.clock.seconds = 1100
			return link

		with patch.object(Transaction, 'get_link', delayed_link):
			result = await self.service.decide(1, ApprovalStatus.APPROVED, '3', '1', '2', '101')
		self.assertFalse(result.accepted)
		self.assertEqual(result.approval.status, ApprovalStatus.TIMED_OUT)

	async def test_cleanup_cascade_no_id_reuse_or_retention_extension(self) -> None:
		await self.create()
		self.clock.seconds = 1119
		await self.service.replace_data(self.owner, 1, b'x', None)
		self.clock.seconds = 1120
		await self.assert_error(404, self.service.get(self.owner, 1))
		self.assertEqual(await self.service.cleanup(), 1)
		async with self.storage.transaction() as transaction:
			for table in ('approval', 'approval_data', 'approval_discord'):
				row = await transaction._one(f'SELECT count(*) FROM {table}')
				self.assertEqual(row[0], 0)
		self.assertEqual((await self.create(expires_at=1200)).approval_id, 2)

	async def test_card_and_restart_dispatch(self) -> None:
		await self.create()
		snapshot = (await self.service.pending_messages())[0]
		card = render_card(snapshot, 'PrimeBackup')
		self.assertEqual(card.title, self.content.title)
		self.assertEqual(card.fields[0].value, 'Steve')
		self.assertIn('待审批', card.footer.text)
		self.assertNotIn(base64.b64encode(snapshot.approval.data).decode(), card.footer.text)
		view = card_view(1, ApprovalStatus.PENDING)
		self.assertTrue(view.is_persistent())
		button = view.children[0]
		self.assertIsInstance(button, DecisionButton)
		restored = await DecisionButton.from_custom_id(None, button.item, button.template.fullmatch(button.custom_id))
		self.assertEqual(restored.approval_id, 1)
		self.assertEqual(restored.decision, ApprovalStatus.APPROVED)
		self.assertTrue(all(child.item.disabled for child in card_view(1, ApprovalStatus.APPROVED).children))

	async def test_current_reviewer_roles_and_no_admin_bypass(self) -> None:
		bot = ApprovalBot(self.config.discord, self.service)
		guild = AsyncMock()
		member = guild.fetch_member.return_value
		member.id = 3
		member.roles = []
		self.assertTrue(await bot.reviewer_allowed(guild, 3))
		member.id = 4
		self.assertFalse(await bot.reviewer_allowed(guild, 4))
		role = type('Role', (), {'id': 5})()
		member.roles = [role]
		bot.config = DiscordConfig(token=SecretStr('token'), guild_id='1', channel_id='2', reviewer_role_ids=['5'])
		self.assertTrue(await bot.reviewer_allowed(guild, 4))
		member.roles = []
		self.assertFalse(await bot.reviewer_allowed(guild, 4))
		self.assertEqual(guild.fetch_member.await_count, 4)
		await bot.close()

	async def test_discord_shutdown_drains_interactions(self) -> None:
		bot = ApprovalBot(self.config.discord, self.service)
		entered = asyncio.Event()

		async def work(*args) -> None:
			async with self.storage.transaction() as transaction:
				await transaction.create('owner', self.content, 1100, b'', '1', '2', 1000)
				entered.set()
				await asyncio.Future()

		with patch.object(bot, '_handle_decision', side_effect=work):
			task = asyncio.create_task(bot.handle_decision(None, 1, ApprovalStatus.APPROVED))
			await entered.wait()
			await bot.close()
			self.assertTrue(task.cancelled())
		self.assertTrue(await self.storage.healthy())
		self.assertEqual(await self.service.list_approvals(self.owner, ApprovalFilter()), [])

	async def test_discord_publish_update_and_missing_message(self) -> None:
		await self.create()
		bot = ApprovalBot(self.config.discord, self.service)
		channel = AsyncMock(spec=discord.TextChannel)
		channel.guild = SimpleNamespace(id=1)
		channel.send.return_value.id = 101
		channel.fetch_message.return_value.id = 101
		with patch.object(bot, 'fetch_channel', return_value=channel):
			snapshot = (await self.service.pending_messages())[0]
			result = await bot.sync(snapshot, 'PrimeBackup')
			self.assertEqual(result.message_id, '101')
			await self.service.complete_message_sync(snapshot, result.message_id)
			await self.service.decide(1, ApprovalStatus.REJECTED, '3', '1', '2', '101')
			snapshot = (await self.service.pending_messages())[0]
			await bot.sync(snapshot, 'PrimeBackup')
			channel.fetch_message.return_value.edit.assert_awaited_once()
			channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), {'code': 10008, 'message': 'Unknown Message'})
			with self.assertLogs('approval_center.discord', level=logging.WARNING):
				result = await bot.sync(snapshot, 'PrimeBackup')
			self.assertTrue(result.missing)
			await self.service.complete_message_sync(snapshot, result.message_id, result.missing)
			self.assertEqual(await self.service.pending_messages(), [])
		await bot.close()

	async def test_sensitive_data_is_not_logged(self) -> None:
		with self.assertLogs('approval_center', level=logging.INFO) as logs:
			approval = await self.service.create(self.owner, self.content, 1100, b'PRIVATE')
			await self.service.replace_data(self.admin, approval.approval_id, b'SECRET', None)
		text = '\n'.join(logs.output)
		self.assertNotIn('PRIVATE', text)
		self.assertNotIn('SECRET', text)
		self.assertNotIn('secret', text)
		self.assertIn('actor_client_id=admin', text)
		self.assertIn('owner_client_id=owner', text)


class ApiTests(ApprovalFixture):
	@override
	async def asyncSetUp(self) -> None:
		await super().asyncSetUp()
		self.app = FastAPI()
		install_api(self.app, self.config, lambda: self.service, lambda: RuntimeHealth(True, True))
		self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False), base_url='http://test', auth=('owner', 'secret'))

	@override
	async def asyncTearDown(self) -> None:
		await self.http.aclose()
		await super().asyncTearDown()

	async def test_api_roundtrip_and_versions(self) -> None:
		response = await self.http.post('/api/v1/approval', json={'content': {'title': '回档'}, 'expires_at': 1100, 'data': 'AP8='})
		self.assertEqual(response.status_code, 201, response.text)
		self.assertEqual(response.json()['approval_id'], 1)
		self.assertEqual(response.json()['created_at'], 1000)
		response = await self.http.get('/api/v1/approval/1')
		self.assertEqual(response.json()['data'], 'AP8=')
		self.assertIsNone(response.json()['decision'])
		response = await self.http.get('/api/v1/approval-data/1')
		self.assertEqual(response.json(), {'data': 'AP8=', 'version': 0, 'updated_at': 1000})
		response = await self.http.put('/api/v1/approval-data/1', json={'data': 'eA==', 'expected_version': 0})
		self.assertEqual(response.json()['version'], 1)
		response = await self.http.put('/api/v1/approval-data/1', json={'data': 'eQ==', 'expected_version': 0})
		self.assertEqual(response.status_code, 409)
		response = await self.http.put('/api/v1/approval-data/1', json={'data': ''})
		self.assertEqual(response.json()['version'], 2)
		response = await self.http.get('/api/v1/approval?limit=1&offset=0')
		self.assertEqual(response.json()['items'][0]['data'], '')
		self.assertNotIn('message_id', response.text)

	async def test_api_authentication_and_admin(self) -> None:
		await self.create()
		for auth in (None, ('owner', 'wrong'), ('unknown', 'secret'), ('disabled', 'secret')):
			response = await self.http.get('/api/v1/approval', auth=auth)
			self.assertEqual(response.status_code, 401)
			self.assertIn('WWW-Authenticate', response.headers)
			self.assertEqual(response.json()['code'], 'authentication_failed')
		for path in ('/api/v1/approval/1', '/api/v1/approval-data/1'):
			self.assertEqual((await self.http.get(path, auth=('other', 'secret'))).status_code, 404)
		self.assertEqual((await self.http.get('/api/v1/approval?all=true')).status_code, 403)
		response = await self.http.get('/api/v1/approval', auth=('admin', 'secret'))
		self.assertEqual(response.json()['items'], [])
		response = await self.http.get('/api/v1/approval?all=true', auth=('admin', 'secret'))
		self.assertEqual(response.json()['items'][0]['client_id'], 'owner')
		response = await self.http.put('/api/v1/approval-data/1', auth=('admin', 'secret'), json={'data': 'YQ=='})
		self.assertEqual(response.status_code, 200)

	async def test_api_reference_key_creation_filter_and_immutability(self) -> None:
		for key in (None, '', '服务器/Steve', '服务器/Steve', '服务器/steve'):
			response = await self.http.post('/api/v1/approval', json={
				'content': {'title': 'Approval'}, 'expires_at': 1100, 'reference_key': key,
			})
			self.assertEqual(response.status_code, 201, response.text)
			self.assertEqual(response.json()['reference_key'], key)
		response = await self.http.get('/api/v1/approval/3')
		self.assertEqual(response.json()['reference_key'], '服务器/Steve')
		response = await self.http.get('/api/v1/approval', params={'reference_key': '服务器/Steve', 'limit': 1, 'offset': 1})
		self.assertEqual([item['approval_id'] for item in response.json()['items']], [3])
		for key, expected in (('服务器/Steve', [4, 3]), ('', [2]), ('null', []), ('%', [])):
			response = await self.http.get('/api/v1/approval', params={'reference_key': key})
			self.assertEqual([item['approval_id'] for item in response.json()['items']], expected)
		response = await self.http.get('/api/v1/approval')
		self.assertEqual(len(response.json()['items']), 5)
		self.assertIsNone(response.json()['items'][-1]['reference_key'])
		response = await self.http.put('/api/v1/approval-data/3', json={'data': '', 'reference_key': 'changed'})
		self.assertEqual(response.status_code, 422)
		self.assertEqual((await self.http.get('/api/v1/approval/3')).json()['reference_key'], '服务器/Steve')
		for key in (123, True, [], {}):
			response = await self.http.post('/api/v1/approval', json={
				'content': {'title': 'Approval'}, 'expires_at': 1100, 'reference_key': key,
			})
			self.assertEqual(response.status_code, 422)

	async def test_api_invalid_inputs_and_health(self) -> None:
		for data in ('!!!', '汉字', 123, None):
			response = await self.http.post('/api/v1/approval', json={'content': {'title': 'x'}, 'expires_at': 1100, 'data': data})
			self.assertEqual(response.status_code, 422)
			self.assertEqual(set(response.json()), {'code', 'message'})
		for expiry in ('1100', 1100.5, True):
			response = await self.http.post('/api/v1/approval', json={'content': {'title': 'x'}, 'expires_at': expiry})
			self.assertEqual(response.status_code, 422)
		for query in ('limit=0', 'limit=1001', 'offset=-1', 'status=unknown', 'created_from=2&created_before=1'):
			self.assertEqual((await self.http.get('/api/v1/approval?' + query)).status_code, 422)
		self.assertEqual((await self.http.get('/api/v1/approval/9223372036854775808')).status_code, 422)
		response = await self.http.get('/heathz', auth=None)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()['status'], 'ok')


class ModelConfigTests(unittest.TestCase):
	def test_content_json_and_capacity(self) -> None:
		content = ApprovalContent(title='回档', fields=(DisplayField(name='玩家', value='Steve', inline=True),))
		self.assertEqual(ApprovalContent.model_validate_json(content.model_dump_json()), content)
		with self.assertRaises(ValidationError):
			ApprovalContent(title='x', fields=tuple(DisplayField(name='x', value='y') for _ in range(26)))
		with self.assertRaises(ValidationError):
			DisplayField(name='x', value='y' * 1025)
		with self.assertRaises(ValidationError):
			ApprovalContent(title='x', description='d' * 4000, fields=(DisplayField(name='x', value='v' * 1000), DisplayField(name='y', value='v' * 1000)))
		with self.assertRaises(ValidationError):
			ApprovalContent(title='   ')
		with self.assertRaises(ValidationError):
			ApprovalContent(title='😀' * 129)

	def test_config_defaults_paths_and_errors(self) -> None:
		config = load_config(Path(__file__).resolve().parents[1] / 'config.example.toml')
		self.assertEqual(config.service.port, 8731)
		self.assertEqual(config.policy.retention_seconds, 180 * 86400)
		self.assertEqual(config.service.database.parent, Path(__file__).resolve().parents[1])
		self.assertNotIn('REPLACE_WITH_BOT_TOKEN', repr(config))
		with self.assertRaises(ValidationError):
			Config(discord=config.discord, clients=[config.clients[0], config.clients[0]])
		with self.assertRaises(ValidationError):
			DiscordConfig(token=SecretStr('token'), guild_id='1', channel_id='2')
		with self.assertRaises(ValidationError):
			PolicyConfig(maintenance_interval=0)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
	async def test_lifespan_health_and_shutdown(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			config = Config(
				service=ServiceConfig(database=Path(directory) / 'center.sqlite3'),
				discord=DiscordConfig(token=SecretStr('token'), guild_id='1', channel_id='2', reviewer_ids=['3']),
				clients=[make_client('owner')], policy=PolicyConfig(maintenance_interval=0.01),
			)
			publisher = FakePublisher(ready=False)
			app = create_app(config, lambda discord_config, service: publisher)
			async with app.router.lifespan_context(app):
				async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
					await asyncio.sleep(0.03)
					response = await client.get('/heathz')
					self.assertEqual(response.status_code, 503)
					self.assertTrue(response.json()['database'])
					publisher.ready = True
					self.assertEqual((await client.get('/heathz')).status_code, 200)
					response = await client.post('/api/v1/approval', auth=('owner', 'secret'), json={'content': {'title': 'x'}, 'expires_at': int(time.time()) + 100})
					self.assertEqual(response.status_code, 201)
			self.assertTrue(publisher.closed)
			self.assertFalse(any(task.get_name() in ('discord', 'maintenance') for task in asyncio.all_tasks() if not task.done()))
			storage = await Storage.open(config.service.database)
			self.assertTrue(await storage.healthy())
			await storage.close()


if __name__ == '__main__':
	unittest.main()
