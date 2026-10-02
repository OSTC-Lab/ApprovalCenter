"""
Source: https://github.com/OSTC-Lab/ApprovalCenter/blob/master/sdk/approval_center_sdk.py
Version: 0.1.0
License: GNU General Public License v3.0 (GPLv3)
License text: https://github.com/OSTC-Lab/ApprovalCenter/blob/master/sdk/LICENSE
"""

import base64
import binascii
from enum import Enum
from types import TracebackType
from typing import Annotated, Optional, TypeVar

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, ValidationError, WithJsonSchema, model_validator

__all__ = [
	'ApprovalStatus', 'DisplayField', 'ApprovalContent', 'DecisionInfo', 'ApprovalInfo',
	'CreateApprovalRequest', 'CreateApprovalResponse',
	'GetApprovalRequest', 'GetApprovalResponse',
	'ListApprovalsRequest', 'ListApprovalsResponse',
	'CancelApprovalRequest', 'CancelApprovalResponse',
	'GetApprovalDataRequest', 'GetApprovalDataResponse',
	'SetApprovalDataRequest', 'SetApprovalDataResponse',
	'ErrorResponse', 'ApprovalCenterAPIError',
	'ApprovalCenterClient', 'AsyncApprovalCenterClient',
]

_MAX_SQLITE_INTEGER = 2 ** 63 - 1
_UnixTime = Annotated[int, Field(strict=True, ge=0, le=_MAX_SQLITE_INTEGER)]
_Response = TypeVar('_Response', bound=BaseModel)


def _decode_data(value: object) -> bytes:
	if isinstance(value, bytes):
		return value
	if not isinstance(value, str):
		raise ValueError('Data must be a Base64 string')
	try:
		return base64.b64decode(value, validate=True)
	except (ValueError, binascii.Error):
		raise ValueError('Data must be a valid Base64 string') from None


def _encode_data(value: bytes) -> str:
	return base64.b64encode(value).decode('ascii')


_Base64Data = Annotated[
	bytes, BeforeValidator(_decode_data), PlainSerializer(_encode_data, return_type=str, when_used='json'),
	WithJsonSchema({'type': 'string', 'contentEncoding': 'base64'}),
]


class _ApiModel(BaseModel):
	# Ignore new response fields so additive server changes remain compatible.
	model_config = ConfigDict(extra='ignore', frozen=True)


class _RequestModel(_ApiModel):
	model_config = ConfigDict(extra='forbid', frozen=True)


class ErrorResponse(_ApiModel):
	code: str
	message: str


class ApprovalCenterAPIError(httpx.HTTPStatusError):
	"""An HTTP failure with a parsed ApprovalCenter error response."""

	def __init__(self, error: ErrorResponse, response: httpx.Response):
		super().__init__(f'{response.status_code} {error.code}: {error.message}', request=response.request, response=response)
		self.status_code = response.status_code
		self.code = error.code
		self.message = error.message


class ApprovalStatus(str, Enum):
	PENDING = 'pending'
	APPROVED = 'approved'
	REJECTED = 'rejected'
	TIMED_OUT = 'timed_out'
	CANCELLED = 'cancelled'


class DisplayField(_ApiModel):
	name: str = Field(min_length=1, max_length=256)
	value: str = Field(min_length=1, max_length=1024)
	inline: bool = False


class ApprovalContent(_ApiModel):
	title: str = Field(min_length=1, max_length=256)
	description: str = Field(default='', max_length=4096)
	fields: tuple[DisplayField, ...] = Field(default=(), max_length=25)

	@model_validator(mode='after')
	def validate_size(self) -> 'ApprovalContent':
		text = [self.title, self.description]
		text.extend(part for field in self.fields for part in (field.name, field.value))
		for part, limit in ((self.title, 256), (self.description, 4096)):
			if len(part.encode('utf-16-le')) // 2 > limit:
				raise ValueError('Display title or description exceeds the single-card capacity')
		for field in self.fields:
			if len(field.name.encode('utf-16-le')) // 2 > 256 or len(field.value.encode('utf-16-le')) // 2 > 1024:
				raise ValueError('Display field exceeds the single-card capacity')
		# Reserve 500 embed characters for center metadata, matching the server.
		if sum(len(part.encode('utf-16-le')) // 2 for part in text) > 5500:
			raise ValueError('Display content exceeds the single-card capacity')
		if not self.title.strip() or any(not field.name.strip() or not field.value.strip() for field in self.fields):
			raise ValueError('Display title, field names and values must not be blank')
		return self


class CreateApprovalRequest(_RequestModel):
	content: ApprovalContent
	expires_at: _UnixTime
	data: _Base64Data = b''
	reference_key: Optional[str] = None


class CreateApprovalResponse(_ApiModel):
	approval_id: int
	reference_key: Optional[str]
	status: ApprovalStatus
	created_at: int
	expires_at: int
	updated_at: int


class DecisionInfo(_ApiModel):
	reviewer_id: Optional[str]
	decided_at: int


class ApprovalInfo(_ApiModel):
	approval_id: int
	reference_key: Optional[str]
	status: ApprovalStatus
	created_at: int
	expires_at: int
	updated_at: int
	client_id: str
	content: ApprovalContent
	decision: Optional[DecisionInfo]
	data: _Base64Data
	data_version: int


class GetApprovalRequest(_RequestModel):
	approval_id: int = Field(strict=True, ge=1, le=_MAX_SQLITE_INTEGER, exclude=True)


class GetApprovalResponse(ApprovalInfo):
	pass


class CancelApprovalRequest(_RequestModel):
	approval_id: int = Field(strict=True, ge=1, le=_MAX_SQLITE_INTEGER, exclude=True)


class CancelApprovalResponse(ApprovalInfo):
	pass


class ListApprovalsRequest(_RequestModel):
	status: Optional[ApprovalStatus] = None
	reference_key: Optional[str] = None
	created_from: Optional[_UnixTime] = None
	created_before: Optional[_UnixTime] = None
	updated_from: Optional[_UnixTime] = None
	updated_before: Optional[_UnixTime] = None
	limit: int = Field(default=100, strict=True, ge=1, le=1000)
	offset: int = Field(default=0, strict=True, ge=0, le=_MAX_SQLITE_INTEGER)
	all_clients: bool = Field(default=False, serialization_alias='all')


class ListApprovalsResponse(_ApiModel):
	items: list[ApprovalInfo]
	limit: int
	offset: int


class GetApprovalDataRequest(_RequestModel):
	approval_id: int = Field(strict=True, ge=1, le=_MAX_SQLITE_INTEGER, exclude=True)


class GetApprovalDataResponse(_ApiModel):
	data: _Base64Data
	version: int
	updated_at: int


class SetApprovalDataRequest(_RequestModel):
	# Path parameters are not part of the JSON body.
	approval_id: int = Field(strict=True, ge=1, le=_MAX_SQLITE_INTEGER, exclude=True)
	data: _Base64Data
	expected_version: Optional[int] = Field(default=None, strict=True, ge=0, le=_MAX_SQLITE_INTEGER)


class SetApprovalDataResponse(_ApiModel):
	version: int
	updated_at: int


def _parse_response(response: httpx.Response, response_type: type[_Response]) -> _Response:
	try:
		response.raise_for_status()
	except httpx.HTTPStatusError as error:
		try:
			body = ErrorResponse.model_validate_json(response.content, strict=True)
		except ValidationError:
			raise error from None
		raise ApprovalCenterAPIError(body, response) from error
	return response_type.model_validate_json(response.content)


class ApprovalCenterClient:
	"""Synchronous client. Reuse an instance and close it when no longer needed."""

	def __init__(self, base_url: str, client_id: str, client_secret: str, *, timeout: float = 10.0):
		self._http = httpx.Client(base_url=base_url.rstrip('/') + '/', auth=(client_id, client_secret), timeout=timeout)

	def __enter__(self) -> 'ApprovalCenterClient':
		self._http.__enter__()
		return self

	def __exit__(self, exc_type: Optional[type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
		self.close()

	def close(self) -> None:
		self._http.close()

	def _request(
		self, method: str, path: str, response_type: type[_Response], *,
		body: Optional[_RequestModel] = None, query: Optional[ListApprovalsRequest] = None,
	) -> _Response:
		response = self._http.request(
			method, path,
			json=None if body is None else body.model_dump(mode='json', exclude_none=True),
			params=None if query is None else query.model_dump(mode='json', by_alias=True, exclude_none=True),
		)
		return _parse_response(response, response_type)

	def create_approval(self, request: CreateApprovalRequest) -> CreateApprovalResponse:
		"""Create an approval. expires_at is the absolute deadline in Unix seconds."""
		return self._request('POST', '/api/v1/approval', CreateApprovalResponse, body=request)

	def get_approval(self, request: GetApprovalRequest) -> GetApprovalResponse:
		return self._request('GET', f'/api/v1/approval/{request.approval_id}', GetApprovalResponse)

	def cancel_approval(self, request: CancelApprovalRequest) -> CancelApprovalResponse:
		"""Cancel a pending approval. Repeated cancellation preserves the terminal time."""
		return self._request('POST', f'/api/v1/approval/{request.approval_id}/cancel', CancelApprovalResponse)

	def list_approvals(self, request: ListApprovalsRequest) -> ListApprovalsResponse:
		"""Fetch one page. Time ranges include their start and exclude their end.

		all_clients requires an administrator client; results are newest first.
		"""
		return self._request('GET', '/api/v1/approval', ListApprovalsResponse, query=request)

	def get_approval_data(self, request: GetApprovalDataRequest) -> GetApprovalDataResponse:
		return self._request('GET', f'/api/v1/approval-data/{request.approval_id}', GetApprovalDataResponse)

	def set_approval_data(self, request: SetApprovalDataRequest) -> SetApprovalDataResponse:
		"""Replace all bytes. Omit expected_version for an unconditional overwrite."""
		return self._request('PUT', f'/api/v1/approval-data/{request.approval_id}', SetApprovalDataResponse, body=request)


class AsyncApprovalCenterClient:
	"""Asynchronous client. Reuse within one event loop and close before shutdown."""

	def __init__(self, base_url: str, client_id: str, client_secret: str, *, timeout: float = 10.0):
		self._http = httpx.AsyncClient(base_url=base_url.rstrip('/') + '/', auth=(client_id, client_secret), timeout=timeout)

	async def __aenter__(self) -> 'AsyncApprovalCenterClient':
		await self._http.__aenter__()
		return self

	async def __aexit__(self, exc_type: Optional[type[BaseException]], exc_value: Optional[BaseException], traceback: Optional[TracebackType]) -> None:
		await self.aclose()

	async def aclose(self) -> None:
		await self._http.aclose()

	async def _request(
		self, method: str, path: str, response_type: type[_Response], *,
		body: Optional[_RequestModel] = None, query: Optional[ListApprovalsRequest] = None,
	) -> _Response:
		response = await self._http.request(
			method, path,
			json=None if body is None else body.model_dump(mode='json', exclude_none=True),
			params=None if query is None else query.model_dump(mode='json', by_alias=True, exclude_none=True),
		)
		return _parse_response(response, response_type)

	async def create_approval(self, request: CreateApprovalRequest) -> CreateApprovalResponse:
		"""Create an approval. expires_at is the absolute deadline in Unix seconds."""
		return await self._request('POST', '/api/v1/approval', CreateApprovalResponse, body=request)

	async def get_approval(self, request: GetApprovalRequest) -> GetApprovalResponse:
		return await self._request('GET', f'/api/v1/approval/{request.approval_id}', GetApprovalResponse)

	async def cancel_approval(self, request: CancelApprovalRequest) -> CancelApprovalResponse:
		"""Cancel a pending approval. Repeated cancellation preserves the terminal time."""
		return await self._request('POST', f'/api/v1/approval/{request.approval_id}/cancel', CancelApprovalResponse)

	async def list_approvals(self, request: ListApprovalsRequest) -> ListApprovalsResponse:
		"""Fetch one page. Time ranges include their start and exclude their end.

		all_clients requires an administrator client; results are newest first.
		"""
		return await self._request('GET', '/api/v1/approval', ListApprovalsResponse, query=request)

	async def get_approval_data(self, request: GetApprovalDataRequest) -> GetApprovalDataResponse:
		return await self._request('GET', f'/api/v1/approval-data/{request.approval_id}', GetApprovalDataResponse)

	async def set_approval_data(self, request: SetApprovalDataRequest) -> SetApprovalDataResponse:
		"""Replace all bytes. Omit expected_version for an unconditional overwrite."""
		return await self._request('PUT', f'/api/v1/approval-data/{request.approval_id}', SetApprovalDataResponse, body=request)
