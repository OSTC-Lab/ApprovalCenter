import base64
import shutil
import subprocess
import sys
import tempfile
import unittest
from functools import partial
from pathlib import Path
from typing import override
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sdk'))

import httpx
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError

import approval_center_sdk as sdk
from approval_center import api
from approval_center.approval import ApprovalContent, ApprovalService, ApprovalStatus, DisplayField
from approval_center.config import ClientConfig, Config, DiscordConfig
from approval_center.storage import Storage

HTTP_CLIENT = httpx.Client
ASYNC_HTTP_CLIENT = httpx.AsyncClient


class SdkTests(unittest.TestCase):
	@override
	def setUp(self) -> None:
		self.requests: list[httpx.Request] = []
		self.responses: list[httpx.Response] = []
		self.created = api.CreateApprovalResponse(
			approval_id=1, reference_key='survival/Steve', status=ApprovalStatus.PENDING, created_at=1000, expires_at=1100, updated_at=1000,
		)
		self.approval = api.ApprovalResponse(
			**self.created.model_dump(), client_id='client', content=ApprovalContent(title='申请', description='理由'),
			decision=None, data=b'\x00\xff', data_version=0,
		)

	def handle(self, request: httpx.Request) -> httpx.Response:
		self.requests.append(request)
		return self.responses.pop(0)

	def make_client(self, base_url: str = 'https://center.example/prefix/') -> sdk.ApprovalCenterClient:
		with patch.object(sdk.httpx, 'Client', partial(HTTP_CLIENT, transport=httpx.MockTransport(self.handle))):
			return sdk.ApprovalCenterClient(base_url, 'client', 'secret')

	def test_sync_api_protocol_and_lifecycle(self) -> None:
		self.responses = [
			httpx.Response(201, content=self.created.model_dump_json()),
			httpx.Response(200, content=self.approval.model_dump_json()),
			httpx.Response(200, content=api.ApprovalListResponse(items=[self.approval], limit=2, offset=3).model_dump_json()),
			httpx.Response(200, content=api.ApprovalDataResponse(data=b'\x00\xff', version=0, updated_at=1000).model_dump_json()),
			httpx.Response(200, content=api.ReplaceDataResponse(version=1, updated_at=1001).model_dump_json()),
			httpx.Response(200, content=api.ReplaceDataResponse(version=2, updated_at=1001).model_dump_json()),
		]
		client = self.make_client()
		with client:
			created = client.create_approval(sdk.CreateApprovalRequest(
				content=sdk.ApprovalContent(title='申请', description='理由'), expires_at=1100, data=b'\x00\xff', reference_key='survival/Steve',
			))
			self.assertEqual(created.approval_id, 1)
			self.assertEqual(created.reference_key, 'survival/Steve')
			self.assertIs(created.status, sdk.ApprovalStatus.PENDING)
			approval = client.get_approval(sdk.ApprovalIdRequest(approval_id=created.approval_id))
			self.assertEqual(approval.data, b'\x00\xff')
			self.assertEqual(approval.content.title, '申请')
			self.assertEqual(approval.reference_key, 'survival/Steve')
			page = client.list_approvals(sdk.ApprovalListRequest(
				status=sdk.ApprovalStatus.PENDING, reference_key='survival/Steve', created_from=1000, created_before=1100,
				updated_from=1001, updated_before=1101, limit=2, offset=3, all_clients=True,
			))
			self.assertEqual(page.items, [approval])
			self.assertEqual(page.limit, 2)
			self.assertEqual(page.offset, 3)
			self.assertEqual(client.get_approval_data(sdk.ApprovalIdRequest(approval_id=1)).data, b'\x00\xff')
			self.assertEqual(client.set_approval_data(sdk.ReplaceDataRequest(approval_id=1, data=b'new', expected_version=0)).version, 1)
			self.assertEqual(client.set_approval_data(sdk.ReplaceDataRequest(approval_id=1, data=b'')).version, 2)
		for request in self.requests:
			self.assertEqual(request.headers['Authorization'], 'Basic ' + base64.b64encode(b'client:secret').decode('ascii'))
			self.assertTrue(request.url.path.startswith('/prefix/api/v1/'))
		create = api.CreateApprovalRequest.model_validate_json(self.requests[0].content)
		self.assertEqual(create.data, b'\x00\xff')
		self.assertEqual(create.content.title, '申请')
		self.assertEqual(create.reference_key, 'survival/Steve')
		self.assertEqual(self.requests[0].method, 'POST')
		self.assertEqual(str(self.requests[2].url.params),
			'status=pending&reference_key=survival%2FSteve&created_from=1000&created_before=1100&updated_from=1001&updated_before=1101&limit=2&offset=3&all=true')
		self.assertEqual(self.requests[3].url.path, '/prefix/api/v1/approval-data/1')
		self.assertEqual(self.requests[4].method, 'PUT')
		self.assertEqual(api.ReplaceDataRequest.model_validate_json(self.requests[4].content).expected_version, 0)
		self.assertIsNone(api.ReplaceDataRequest.model_validate_json(self.requests[5].content).expected_version)
		with self.assertRaises(RuntimeError):
			client.get_approval(sdk.ApprovalIdRequest(approval_id=1))

	def test_http_errors_preserve_response_without_retry(self) -> None:
		cases = (
			(401, 'authentication_failed'), (403, 'forbidden'), (404, 'approval_not_found'),
			(409, 'data_version_conflict'), (409, 'approval_not_pending'), (422, 'invalid_request'), (500, 'internal_error'),
		)
		for status, code in cases:
			with self.subTest(status=status, code=code):
				self.responses = [httpx.Response(status, content=api.ErrorResponse(code=code, message='details').model_dump_json())]
				with self.make_client() as client:
					with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
						client.set_approval_data(sdk.ReplaceDataRequest(approval_id=1, data=b'data', expected_version=0))
				self.assertIsInstance(caught.exception, httpx.HTTPStatusError)
				self.assertEqual(caught.exception.status_code, status)
				self.assertEqual(caught.exception.code, code)
				self.assertEqual(caught.exception.message, 'details')
				self.assertEqual(caught.exception.response.status_code, status)
				self.assertEqual(caught.exception.response.json()['message'], 'details')
				self.assertIs(caught.exception.request, self.requests[-1])
				self.assertIs(caught.exception.response, caught.exception.__cause__.response)
		self.assertEqual(len(self.requests), len(cases))

	def test_unstructured_http_errors_keep_httpx_exception(self) -> None:
		for content in (
			'<html>Bad Gateway</html>', '{', '{}', 'null', '[]',
			'{"code": "error"}', '{"code": 123, "message": "details"}', '{"code": "error", "message": null}',
		):
			with self.subTest(content=content):
				self.responses = [httpx.Response(502, content=content)]
				with self.make_client() as client:
					with self.assertRaises(httpx.HTTPStatusError) as caught:
						client.get_approval(sdk.ApprovalIdRequest(approval_id=1))
				self.assertNotIsInstance(caught.exception, sdk.ApprovalCenterAPIError)
				self.assertEqual(caught.exception.response.status_code, 502)
				self.assertEqual(caught.exception.response.text, content)

	def test_sync_cancel_protocol(self) -> None:
		cancelled = self.approval.model_copy(update={
			'status': ApprovalStatus.CANCELLED, 'decision': api.DecisionInfo(reviewer_id=None, decided_at=1001), 'updated_at': 1001,
		})
		self.responses = [httpx.Response(200, content=cancelled.model_dump_json())]
		with self.make_client() as client:
			response = client.cancel_approval(sdk.ApprovalIdRequest(approval_id=1))
		self.assertIs(response.status, sdk.ApprovalStatus.CANCELLED)
		self.assertEqual(response.decision.decided_at, 1001)
		self.assertIsNone(response.decision.reviewer_id)
		self.assertEqual(response.data, b'\x00\xff')
		self.assertEqual(self.requests[0].method, 'POST')
		self.assertEqual(self.requests[0].url.path, '/prefix/api/v1/approval/1/cancel')
		self.assertEqual(self.requests[0].content, b'')

	def test_transport_error_is_not_wrapped_or_retried(self) -> None:
		error = httpx.ReadTimeout('Simulated timeout')

		def fail(request: httpx.Request) -> httpx.Response:
			raise error

		with patch.object(sdk.httpx, 'Client', partial(HTTP_CLIENT, transport=httpx.MockTransport(fail))):
			with sdk.ApprovalCenterClient('https://center.example', 'client', 'secret') as client:
				with patch.object(client._http, 'request', wraps=client._http.request) as request:
					with self.assertRaises(httpx.ReadTimeout) as caught:
						client.create_approval(sdk.CreateApprovalRequest(content=sdk.ApprovalContent(title='Approval'), expires_at=1100))
					self.assertIs(caught.exception, error)
					self.assertEqual(request.call_count, 1)

	def test_response_validation_and_additive_fields(self) -> None:
		payload = self.approval.model_dump(mode='json')
		payload['future_field'] = 'ignored'
		payload['content']['future_field'] = 'ignored'
		self.responses = [
			httpx.Response(200, json=payload),
			httpx.Response(200, content='not JSON'),
			httpx.Response(200, json={'data': 'invalid!', 'version': 0, 'updated_at': 1000}),
		]
		with self.make_client() as client:
			self.assertEqual(client.get_approval(sdk.ApprovalIdRequest(approval_id=1)).data, b'\x00\xff')
			with self.assertRaises(ValidationError):
				client.get_approval(sdk.ApprovalIdRequest(approval_id=1))
			with self.assertRaises(ValidationError):
				client.get_approval_data(sdk.ApprovalIdRequest(approval_id=1))

	def test_content_validation_matches_server(self) -> None:
		for title in ('Approval', '审批', '😀' * 128, '😀' * 129, ' ', 'x' * 257):
			with self.subTest(title=title):
				try:
					server = ApprovalContent(title=title)
				except ValidationError:
					with self.assertRaises(ValidationError):
						sdk.ApprovalContent(title=title)
				else:
					self.assertEqual(sdk.ApprovalContent(title=title).model_dump_json(), server.model_dump_json())
		for model, field_type in ((ApprovalContent, DisplayField), (sdk.ApprovalContent, sdk.DisplayField)):
			with self.assertRaises(ValidationError):
				model(title='Approval', description='x' * 4096, fields=tuple(field_type(name='x', value='x' * 1024) for _ in range(2)))

	def test_copy_is_independent_of_server(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			shutil.copyfile(Path(sdk.__file__), Path(directory) / 'approval_center_sdk.py')
			result = subprocess.run(
				[sys.executable, '-I', '-c',
					"import sys; sys.path.insert(0, '.'); import approval_center_sdk as sdk; "
					"body = sdk.CreateApprovalRequest(content=sdk.ApprovalContent(title='Test'), expires_at=1000); "
					"assert body.model_dump(mode='json')['data'] == ''; "
					"assert not any(name == 'approval_center' or name.startswith('approval_center.') for name in sys.modules); "
					"assert not hasattr(sdk.ApprovalCenterClient, 'get_health')"],
				cwd=directory, capture_output=True, text=True,
			)
			self.assertEqual(result.returncode, 0, result.stderr)


class AsyncSdkTests(unittest.IsolatedAsyncioTestCase):
	@override
	async def asyncSetUp(self) -> None:
		self.temp = tempfile.TemporaryDirectory()
		self.storage = await Storage.open(Path(self.temp.name) / 'test.sqlite3')
		self.now = 1000
		self.owner = ClientConfig(client_id='owner', client_secret=SecretStr('secret'), display_name='Owner')
		self.other = ClientConfig(client_id='other', client_secret=SecretStr('secret'), display_name='Other')
		self.admin = ClientConfig(client_id='admin', client_secret=SecretStr('secret'), display_name='Admin', is_admin=True)
		self.config = Config(
			discord=DiscordConfig(token=SecretStr('token'), guild_id='1', channel_id='2', reviewer_ids=['3']),
			clients=[self.owner, self.other, self.admin],
		)
		self.service = ApprovalService(self.storage, self.config.policy, '1', '2', lambda: float(self.now))
		self.app = FastAPI()
		api.install_api(self.app, self.config, lambda: self.service, lambda: api.RuntimeHealth(True, True))

	@override
	async def asyncTearDown(self) -> None:
		await self.storage.close()
		self.temp.cleanup()

	def make_client(self, client_id: str = 'owner', secret: str = 'secret') -> sdk.AsyncApprovalCenterClient:
		with patch.object(sdk.httpx, 'AsyncClient', partial(ASYNC_HTTP_CLIENT, transport=httpx.ASGITransport(app=self.app))):
			return sdk.AsyncApprovalCenterClient('http://center.example', client_id, secret)

	async def test_actual_api_create_read_list_and_data(self) -> None:
		client = self.make_client()
		async with client:
			created = await client.create_approval(sdk.CreateApprovalRequest(
				content=sdk.ApprovalContent(title='申请回档', fields=(sdk.DisplayField(name='玩家', value='Steve'),)),
				expires_at=1100, data=b'\x00\xff',
			))
			request = sdk.ApprovalIdRequest(approval_id=created.approval_id)
			approval = await client.get_approval(request)
			self.assertEqual(approval.data, b'\x00\xff')
			self.assertEqual(approval.content.fields[0].value, 'Steve')
			self.assertIsNone(approval.decision)
			page = await client.list_approvals(sdk.ApprovalListRequest(status=sdk.ApprovalStatus.PENDING, created_from=1000, created_before=1001))
			self.assertEqual(page.items, [approval])
			self.assertEqual((await client.list_approvals(sdk.ApprovalListRequest(offset=1))).items, [])
			data = await client.get_approval_data(request)
			self.assertEqual(data.data, b'\x00\xff')
			self.assertEqual(data.version, 0)
			self.now = 1001
			replaced = await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b'new', expected_version=data.version))
			self.assertEqual(replaced.version, 1)
			self.assertEqual(replaced.updated_at, 1001)
			with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
				await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b'stale', expected_version=0))
			self.assertEqual(caught.exception.response.status_code, 409)
			self.assertEqual(caught.exception.code, 'data_version_conflict')
			self.assertEqual(caught.exception.message, 'Custom data version does not match')
			self.assertEqual((await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b''))).version, 2)
			self.assertEqual((await client.get_approval_data(request)).data, b'')
			self.now = 1100
			timed_out = await client.get_approval(request)
			self.assertIs(timed_out.status, sdk.ApprovalStatus.TIMED_OUT)
			self.assertIsNotNone(timed_out.decision)
			self.assertIsNone(timed_out.decision.reviewer_id)
			self.assertEqual(timed_out.decision.decided_at, 1100)
			self.assertEqual((await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b'terminal'))).version, 3)
		with self.assertRaises(RuntimeError):
			await client.get_approval(request)

	async def test_actual_api_auth_and_admin_permissions(self) -> None:
		async with self.make_client() as owner:
			created = await owner.create_approval(sdk.CreateApprovalRequest(content=sdk.ApprovalContent(title='Approval'), expires_at=1100))
			request = sdk.ApprovalIdRequest(approval_id=created.approval_id)
			with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
				await owner.list_approvals(sdk.ApprovalListRequest(all_clients=True))
			self.assertEqual(caught.exception.response.status_code, 403)
		async with self.make_client('other') as other:
			with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
				await other.get_approval(request)
			self.assertEqual(caught.exception.response.status_code, 404)
		async with self.make_client('owner', 'wrong') as invalid:
			with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
				await invalid.get_approval(request)
			self.assertEqual(caught.exception.response.status_code, 401)
		async with self.make_client('admin') as admin:
			self.assertEqual((await admin.list_approvals(sdk.ApprovalListRequest())).items, [])
			self.assertEqual(len((await admin.list_approvals(sdk.ApprovalListRequest(all_clients=True))).items), 1)
			self.assertEqual((await admin.get_approval(request)).client_id, 'owner')
			self.assertEqual((await admin.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b'admin'))).version, 1)

	async def test_actual_api_reference_key_query_semantics(self) -> None:
		async with self.make_client() as client:
			first = await client.create_approval(sdk.CreateApprovalRequest(content=sdk.ApprovalContent(title='Unkeyed'), expires_at=1100))
			self.assertIsNone(first.reference_key)
			key = 'survival/玩家 ?&%'
			second = await client.create_approval(sdk.CreateApprovalRequest(
				content=sdk.ApprovalContent(title='Keyed'), expires_at=1100, reference_key=key,
			))
			self.assertEqual(second.reference_key, key)
			page = await client.list_approvals(sdk.ApprovalListRequest(reference_key=key))
			self.assertEqual([item.approval_id for item in page.items], [second.approval_id])
			self.assertEqual(page.items[0].reference_key, key)
			page = await client.list_approvals(sdk.ApprovalListRequest(reference_key=None))
			self.assertEqual(len(page.items), 2)
			self.assertIsNone(page.items[-1].reference_key)
			self.assertEqual((await client.list_approvals(sdk.ApprovalListRequest(reference_key='null'))).items, [])
			await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=second.approval_id, data=b'updated'))
			self.assertEqual((await client.get_approval(sdk.ApprovalIdRequest(approval_id=second.approval_id))).reference_key, key)

	async def test_actual_api_cancel(self) -> None:
		async with self.make_client() as client:
			created = await client.create_approval(sdk.CreateApprovalRequest(
				content=sdk.ApprovalContent(title='Approval'), expires_at=1100, data=b'\xff', reference_key='survival/Steve',
			))
			request = sdk.ApprovalIdRequest(approval_id=created.approval_id)
			async with self.make_client('other') as other:
				with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
					await other.cancel_approval(request)
				self.assertEqual(caught.exception.response.status_code, 404)
			self.now = 1001
			cancelled = await client.cancel_approval(request)
			self.assertIs(cancelled.status, sdk.ApprovalStatus.CANCELLED)
			self.assertEqual(cancelled.decision.decided_at, 1001)
			self.assertIsNone(cancelled.decision.reviewer_id)
			self.assertEqual(cancelled.data, b'\xff')
			self.assertEqual(cancelled.reference_key, 'survival/Steve')
			self.now = 1002
			self.assertEqual(await client.cancel_approval(request), cancelled)
			page = await client.list_approvals(sdk.ApprovalListRequest(status=sdk.ApprovalStatus.CANCELLED))
			self.assertEqual(page.items, [cancelled])
			self.assertEqual((await client.set_approval_data(sdk.ReplaceDataRequest(approval_id=created.approval_id, data=b'new'))).version, 1)
			pending = await client.create_approval(sdk.CreateApprovalRequest(content=sdk.ApprovalContent(title='Timeout'), expires_at=1100))
			self.now = 1100
			with self.assertRaises(sdk.ApprovalCenterAPIError) as caught:
				await client.cancel_approval(sdk.ApprovalIdRequest(approval_id=pending.approval_id))
			self.assertEqual(caught.exception.status_code, 409)
			self.assertEqual(caught.exception.code, 'approval_not_pending')

	async def test_async_transport_and_response_errors(self) -> None:
		error = httpx.ConnectError('Simulated connection failure')

		def fail(request: httpx.Request) -> httpx.Response:
			raise error

		with patch.object(sdk.httpx, 'AsyncClient', partial(ASYNC_HTTP_CLIENT, transport=httpx.MockTransport(fail))):
			async with sdk.AsyncApprovalCenterClient('https://center.example', 'client', 'secret') as client:
				with self.assertRaises(httpx.ConnectError) as caught:
					await client.get_approval(sdk.ApprovalIdRequest(approval_id=1))
				self.assertIs(caught.exception, error)
		with patch.object(sdk.httpx, 'AsyncClient', partial(ASYNC_HTTP_CLIENT, transport=httpx.MockTransport(lambda request: httpx.Response(200, content='not JSON')))):
			async with sdk.AsyncApprovalCenterClient('https://center.example', 'client', 'secret') as client:
				with self.assertRaises(ValidationError):
					await client.get_approval(sdk.ApprovalIdRequest(approval_id=1))
