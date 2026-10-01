import argparse
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Protocol

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from approval_center.api import RuntimeHealth, install_api
from approval_center.approval import ApprovalService
from approval_center.config import Config, DiscordConfig, load_config
from approval_center.discord import ApprovalBot
from approval_center.maintenance import Maintenance, MessagePublisher
from approval_center.storage import Storage

LOGGER = logging.getLogger(__name__)


class RuntimeBot(MessagePublisher, Protocol):
	async def start(self, token: str) -> None: ...

	async def close(self) -> None: ...


@dataclass(frozen=True)
class RuntimeResources:
	service: ApprovalService
	bot: RuntimeBot
	maintenance: Maintenance
	bot_task: asyncio.Task[None]
	maintenance_task: asyncio.Task[None]


def create_app(config: Config, bot_factory: Callable[[DiscordConfig, ApprovalService], RuntimeBot] = ApprovalBot) -> FastAPI:
	resources: RuntimeResources | None = None

	def get_service() -> ApprovalService:
		if resources is None:
			raise RuntimeError('Approval service is not running')
		return resources.service

	def get_health() -> RuntimeHealth:
		if resources is None:
			return RuntimeHealth(False, False)
		return RuntimeHealth(
			discord_ready=resources.bot.ready and not resources.bot_task.done(),
			maintenance_healthy=resources.maintenance.healthy and not resources.maintenance_task.done(),
		)

	async def run_bot(bot: RuntimeBot) -> None:
		try:
			await bot.start(config.discord.token.get_secret_value())
		except Exception:
			LOGGER.exception('Discord task stopped; HTTP remains available with degraded health')

	@asynccontextmanager
	async def lifespan(app: FastAPI) -> AsyncIterator[None]:
		nonlocal resources
		storage = await Storage.open(config.service.database)
		bot: RuntimeBot | None = None
		bot_task: asyncio.Task[None] | None = None
		maintenance_task: asyncio.Task[None] | None = None
		try:
			service = ApprovalService(storage, config.policy, config.discord.guild_id, config.discord.channel_id)
			bot = bot_factory(config.discord, service)
			maintenance = Maintenance(service, bot, config)
			bot_task = asyncio.create_task(run_bot(bot), name='discord')
			maintenance_task = asyncio.create_task(maintenance.run(), name='maintenance')
			resources = RuntimeResources(service, bot, maintenance, bot_task, maintenance_task)
			LOGGER.info('ApprovalCenter started')
			yield
		finally:
			try:
				if maintenance_task is not None:
					maintenance_task.cancel()
					await asyncio.gather(maintenance_task, return_exceptions=True)
				if bot is not None:
					await bot.close()
			finally:
				if bot_task is not None:
					bot_task.cancel()
					await asyncio.gather(bot_task, return_exceptions=True)
				resources = None
				await storage.close()
				LOGGER.info('ApprovalCenter stopped')

	app = FastAPI(title='ApprovalCenter', version='0.1.0', lifespan=lifespan)
	install_api(app, config, get_service, get_health)
	return app


def main() -> None:
	parser = argparse.ArgumentParser(description='ApprovalCenter HTTP and Discord service')
	parser.add_argument('--config', type=Path, default=Path('config.toml'), help='Configuration TOML path')
	arguments = parser.parse_args()
	try:
		config = load_config(arguments.config)
	except ValidationError as error:
		# Pydantic's default exception text can include raw credential input.
		parser.exit(2, f'Invalid configuration: {error.errors(include_input=False, include_context=False)}\n')
	except (OSError, ValueError) as error:
		parser.exit(2, f'Unable to load configuration ({type(error).__name__})\n')
	logging.basicConfig(level=config.logging.level, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
	uvicorn.run(create_app(config), host=config.service.host, port=config.service.port, log_config=None, access_log=False)
