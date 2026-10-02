import base64
import binascii
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Callable, Literal, Mapping, Self

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, WithJsonSchema
from starlette.exceptions import HTTPException as StarletteHTTPException

from approval_center.approval import Approval, ApprovalContent, ApprovalError, ApprovalFilter, ApprovalService, ApprovalStatus
from approval_center.config import ClientConfig, Config

LOGGER = logging.getLogger(__name__)
MAX_SQLITE_INTEGER = 2 ** 63 - 1
UnixTime = Annotated[int, Field(strict=True, ge=0, le=MAX_SQLITE_INTEGER)]


def decode_data(value: object) -> bytes:
	if isinstance(value, bytes):
		return value
	if not isinstance(value, str):
		raise ValueError('Data must be a Base64 string')
	try:
		return base64.b64decode(value, validate=True)
	except (ValueError, binascii.Error):
		raise ValueError('Data must be a valid Base64 string') from None


def encode_data(value: bytes) -> str:
	return base64.b64encode(value).decode('ascii')


Base64Data = Annotated[
	bytes, BeforeValidator(decode_data), PlainSerializer(encode_data, return_type=str, when_used='json'),
	WithJsonSchema({'type': 'string', 'contentEncoding': 'base64'}),
]


class ApiModel(BaseModel):
	model_config = ConfigDict(extra='forbid', frozen=True)


class CreateApprovalRequest(ApiModel):
	content: ApprovalContent
	expires_at: UnixTime
	data: Base64Data = b''
	reference_key: str | None = None


class CreateApprovalResponse(ApiModel):
	approval_id: int
	reference_key: str | None
	status: ApprovalStatus
	created_at: int
	expires_at: int
	updated_at: int


class DecisionInfo(ApiModel):
	reviewer_name: str | None
	decided_at: int


class ApprovalInfo(ApiModel):
	approval_id: int
	reference_key: str | None
	status: ApprovalStatus
	created_at: int
	expires_at: int
	updated_at: int
	client_id: str
	content: ApprovalContent
	decision: DecisionInfo | None
	data: Base64Data
	data_version: int

	@classmethod
	def of(cls, approval: Approval) -> Self:
		decision = None if approval.decided_at is None else DecisionInfo(reviewer_name=approval.reviewer_name, decided_at=approval.decided_at)
		return cls(
			approval_id=approval.approval_id, status=approval.status, client_id=approval.client_id, reference_key=approval.reference_key,
			content=approval.content, created_at=approval.created_at, expires_at=approval.expires_at,
			updated_at=approval.updated_at, decision=decision, data=approval.data, data_version=approval.data_version,
		)


class GetApprovalRequest(ApiModel):
	approval_id: int = Field(ge=1, le=MAX_SQLITE_INTEGER)


class GetApprovalResponse(ApprovalInfo):
	pass


class CancelApprovalRequest(ApiModel):
	approval_id: int = Field(ge=1, le=MAX_SQLITE_INTEGER)


class CancelApprovalResponse(ApprovalInfo):
	pass


class SetApprovalStatusRequest(ApiModel):
	status: ApprovalStatus


class SetApprovalStatusResponse(ApprovalInfo):
	pass


class ListApprovalsRequest(ApiModel):
	model_config = ConfigDict(extra='ignore', frozen=True, populate_by_name=True)
	status: ApprovalStatus | None = None
	reference_key: str | None = None
	created_from: int | None = Field(default=None, ge=0, le=MAX_SQLITE_INTEGER)
	created_before: int | None = Field(default=None, ge=0, le=MAX_SQLITE_INTEGER)
	updated_from: int | None = Field(default=None, ge=0, le=MAX_SQLITE_INTEGER)
	updated_before: int | None = Field(default=None, ge=0, le=MAX_SQLITE_INTEGER)
	limit: int = Field(default=100, ge=1, le=1000)
	offset: int = Field(default=0, ge=0, le=MAX_SQLITE_INTEGER)
	all_clients: bool = Field(default=False, alias='all')


class ListApprovalsResponse(ApiModel):
	items: list[ApprovalInfo]
	limit: int
	offset: int


class GetApprovalDataRequest(ApiModel):
	approval_id: int = Field(ge=1, le=MAX_SQLITE_INTEGER)


class GetApprovalDataResponse(ApiModel):
	data: Base64Data
	version: int
	updated_at: int


class SetApprovalDataRequest(ApiModel):
	data: Base64Data
	expected_version: int | None = Field(default=None, strict=True, ge=0, le=MAX_SQLITE_INTEGER)


class SetApprovalDataResponse(ApiModel):
	version: int
	updated_at: int


class ErrorResponse(ApiModel):
	code: str
	message: str


class GetHealthRequest(ApiModel):
	pass


class GetHealthResponse(ApiModel):
	status: Literal['ok', 'degraded']
	service: bool
	database: bool
	discord: bool
	maintenance: bool


@dataclass(frozen=True)
class RuntimeHealth:
	discord_ready: bool
	maintenance_healthy: bool


def error_response(code: str, message: str, status_code: int, headers: Mapping[str, str] | None = None) -> JSONResponse:
	return JSONResponse(ErrorResponse(code=code, message=message).model_dump(mode='json'), status_code=status_code, headers=headers)


def install_api(app: FastAPI, config: Config, service_provider: Callable[[], ApprovalService], health_provider: Callable[[], RuntimeHealth]) -> None:
	security = HTTPBasic(auto_error=False)

	async def authenticate(credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> ClientConfig:
		if credentials is not None:
			client = config.find_client(credentials.username)
			if client is not None and client.enabled:
				if secrets.compare_digest(credentials.password.encode('utf-8'), client.client_secret.get_secret_value().encode('utf-8')):
					return client
		raise HTTPException(status_code=401, detail='Authentication failed', headers={'WWW-Authenticate': 'Basic'})

	Client = Annotated[ClientConfig, Depends(authenticate)]
	ApprovalId = Annotated[int, Path(ge=1, le=MAX_SQLITE_INTEGER)]

	@app.get('/admin', include_in_schema=False)
	async def get_admin() -> FileResponse:
		return FileResponse(FilePath(__file__).with_name('admin.html'), headers={'Cache-Control': 'no-store'})

	@app.exception_handler(ApprovalError)
	async def approval_error_handler(request: Request, error: ApprovalError) -> JSONResponse:
		return error_response(error.code, error.message, error.status_code)

	@app.exception_handler(RequestValidationError)
	async def validation_error_handler(request: Request, error: RequestValidationError) -> JSONResponse:
		# Do not include rejected input values: they may contain credentials or private data.
		return error_response('invalid_request', 'Request parameters or body are invalid', 422)

	@app.exception_handler(StarletteHTTPException)
	async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
		code = 'authentication_failed' if error.status_code == 401 else 'http_error'
		return error_response(code, str(error.detail), error.status_code, error.headers)

	@app.exception_handler(Exception)
	async def internal_error_handler(request: Request, error: Exception) -> JSONResponse:
		LOGGER.exception('HTTP request failed method=%s path=%s', request.method, request.url.path)
		return error_response('internal_error', 'Internal service error', 500)

	@app.post('/api/v1/approval', response_model=CreateApprovalResponse, status_code=201)
	async def create_approval(body: CreateApprovalRequest, client: Client) -> CreateApprovalResponse:
		approval = await service_provider().create(client, body.content, body.expires_at, body.data, reference_key=body.reference_key)
		return CreateApprovalResponse(
			approval_id=approval.approval_id, reference_key=approval.reference_key, status=approval.status, created_at=approval.created_at,
			expires_at=approval.expires_at, updated_at=approval.updated_at,
		)

	@app.get('/api/v1/approval', response_model=ListApprovalsResponse)
	async def list_approvals(
		request: Annotated[ListApprovalsRequest, Query()], client: Client,
	) -> ListApprovalsResponse:
		filters = ApprovalFilter(
			status=request.status, reference_key=request.reference_key,
			created_from=request.created_from, created_before=request.created_before,
			updated_from=request.updated_from, updated_before=request.updated_before,
			limit=request.limit, offset=request.offset, all_clients=request.all_clients,
		)
		items = await service_provider().list_approvals(client, filters)
		return ListApprovalsResponse(items=[ApprovalInfo.of(item) for item in items], limit=request.limit, offset=request.offset)

	@app.get('/api/v1/approval/{approval_id}', response_model=GetApprovalResponse)
	async def get_approval(request: Annotated[GetApprovalRequest, Depends()], client: Client) -> GetApprovalResponse:
		return GetApprovalResponse.of(await service_provider().get(client, request.approval_id))

	@app.post('/api/v1/approval/{approval_id}/cancel', response_model=CancelApprovalResponse)
	async def cancel_approval(request: Annotated[CancelApprovalRequest, Depends()], client: Client) -> CancelApprovalResponse:
		return CancelApprovalResponse.of(await service_provider().cancel(client, request.approval_id))

	@app.put('/api/v1/approval/{approval_id}/status', response_model=SetApprovalStatusResponse)
	async def set_approval_status(approval_id: ApprovalId, body: SetApprovalStatusRequest, client: Client) -> SetApprovalStatusResponse:
		return SetApprovalStatusResponse.of(await service_provider().set_status(client, approval_id, body.status))

	@app.get('/api/v1/approval-data/{approval_id}', response_model=GetApprovalDataResponse)
	async def get_approval_data(request: Annotated[GetApprovalDataRequest, Depends()], client: Client) -> GetApprovalDataResponse:
		approval = await service_provider().get(client, request.approval_id)
		return GetApprovalDataResponse(data=approval.data, version=approval.data_version, updated_at=approval.updated_at)

	@app.put('/api/v1/approval-data/{approval_id}', response_model=SetApprovalDataResponse)
	async def set_approval_data(approval_id: ApprovalId, body: SetApprovalDataRequest, client: Client) -> SetApprovalDataResponse:
		approval = await service_provider().replace_data(client, approval_id, body.data, body.expected_version)
		return SetApprovalDataResponse(version=approval.data_version, updated_at=approval.updated_at)

	@app.get('/heathz', response_model=GetHealthResponse)
	async def get_health(request: Annotated[GetHealthRequest, Depends()]) -> JSONResponse:
		database = await service_provider().storage.healthy()
		runtime = health_provider()
		ok = database and runtime.discord_ready and runtime.maintenance_healthy
		response = GetHealthResponse(status='ok' if ok else 'degraded', service=True, database=database, discord=runtime.discord_ready, maintenance=runtime.maintenance_healthy)
		return JSONResponse(response.model_dump(mode='json'), status_code=200 if ok else 503)
