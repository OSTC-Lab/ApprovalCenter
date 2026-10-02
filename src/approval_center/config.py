import logging
import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

Snowflake = Annotated[str, Field(pattern=r'^[1-9][0-9]{0,19}$')]


class ConfigModel(BaseModel):
	model_config = ConfigDict(extra='forbid', frozen=True)


class ServiceConfig(ConfigModel):
	host: str = '127.0.0.1'
	port: int = Field(default=8731, ge=1, le=65535)
	database: Path = Path('approval_center.sqlite3')


class DiscordConfig(ConfigModel):
	token: SecretStr
	proxy_url: SecretStr | None = None
	guild_id: Snowflake
	channel_id: Snowflake
	reviewer_ids: list[Snowflake] = Field(default_factory=list)
	reviewer_role_ids: list[Snowflake] = Field(default_factory=list)

	@field_validator('proxy_url')
	@classmethod
	def validate_proxy_url(cls, value: SecretStr | None) -> SecretStr | None:
		if value is None:
			return None
		url = value.get_secret_value()
		if not url.lower().startswith('http://') or any(character.isspace() or ord(character) < 32 for character in url):
			raise ValueError('Discord proxy_url must be an http:// proxy URL')
		try:
			proxy = AnyHttpUrl(url)
			address = urlsplit(url)
		except (ValidationError, ValueError):
			raise ValueError('Discord proxy_url must have a valid host and port') from None
		if proxy.port == 0 or address.netloc.endswith(':'):
			raise ValueError('Discord proxy_url port must be between 1 and 65535')
		if address.path not in ('', '/') or proxy.query is not None or proxy.fragment is not None:
			raise ValueError('Discord proxy_url must not contain a path, query or fragment')
		return SecretStr(str(proxy))

	@model_validator(mode='after')
	def validate_credentials(self) -> 'DiscordConfig':
		if not self.token.get_secret_value().strip():
			raise ValueError('Discord token must not be empty')
		if not self.reviewer_ids and not self.reviewer_role_ids:
			raise ValueError('At least one reviewer or reviewer role must be configured')
		return self


class ClientConfig(ConfigModel):
	client_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.-]+$')
	client_secret: SecretStr
	display_name: str = Field(min_length=1, max_length=256)
	enabled: bool = True
	is_admin: bool = False

	@field_validator('display_name')
	@classmethod
	def validate_display_name(cls, value: str) -> str:
		if not value.strip() or len(value.encode('utf-16-le')) // 2 > 256:
			raise ValueError('Client display name exceeds the card capacity or is blank')
		return value

	@field_validator('client_secret')
	@classmethod
	def validate_secret(cls, value: SecretStr) -> SecretStr:
		if not value.get_secret_value():
			raise ValueError('Client secret must not be empty')
		return value


class PolicyConfig(ConfigModel):
	maintenance_interval: float = Field(default=5, gt=0)
	max_approval_seconds: int = Field(default=7 * 86400, gt=0)
	retention_seconds: int = Field(default=180 * 86400, gt=0)
	max_data_bytes: int = Field(default=1024 * 1024, gt=0)


class LoggingConfig(ConfigModel):
	level: str = 'INFO'

	@field_validator('level')
	@classmethod
	def validate_level(cls, value: str) -> str:
		value = value.upper()
		if value not in logging.getLevelNamesMapping():
			raise ValueError('Unknown logging level')
		return value


class Config(ConfigModel):
	service: ServiceConfig = Field(default_factory=ServiceConfig)
	discord: DiscordConfig
	clients: list[ClientConfig] = Field(min_length=1)
	policy: PolicyConfig = Field(default_factory=PolicyConfig)
	logging: LoggingConfig = Field(default_factory=LoggingConfig)

	@model_validator(mode='after')
	def validate_clients(self) -> 'Config':
		ids = [client.client_id for client in self.clients]
		if len(ids) != len(set(ids)):
			raise ValueError('Duplicate client_id')
		return self

	def find_client(self, client_id: str) -> ClientConfig | None:
		return next((client for client in self.clients if client.client_id == client_id), None)


def load_config(path: Path) -> Config:
	path = path.resolve()
	with path.open('rb') as file:
		config = Config.model_validate(tomllib.load(file))
	database = config.service.database
	if not database.is_absolute():
		database = path.parent / database
	return config.model_copy(update={'service': config.service.model_copy(update={'database': database.resolve()})})
