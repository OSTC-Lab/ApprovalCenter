import asyncio
import logging
import re

import discord

from approval_center.approval import ApprovalError, ApprovalService, ApprovalStatus, MessageSnapshot, MessageSyncResult
from approval_center.config import DiscordConfig

LOGGER = logging.getLogger(__name__)


def status_text(status: ApprovalStatus) -> str:
	match status:
		case ApprovalStatus.PENDING:
			return '待审批'
		case ApprovalStatus.APPROVED:
			return '已同意'
		case ApprovalStatus.REJECTED:
			return '已拒绝'
		case ApprovalStatus.TIMED_OUT:
			return '已超时'


def render_card(snapshot: MessageSnapshot, display_name: str) -> discord.Embed:
	approval = snapshot.approval
	color = {
		ApprovalStatus.PENDING: 0x3498DB,
		ApprovalStatus.APPROVED: 0x2ECC71,
		ApprovalStatus.REJECTED: 0xE74C3C,
		ApprovalStatus.TIMED_OUT: 0x95A5A6,
	}[approval.status]
	embed = discord.Embed(title=approval.content.title, description=approval.content.description or None, color=color)
	embed.set_author(name=display_name)
	for field in approval.content.fields:
		embed.add_field(name=field.name, value=field.value, inline=field.inline)
	footer = f'单号：{approval.approval_id} | 状态：{status_text(approval.status)} | 截止时间：{approval.expires_at}'
	if approval.decided_at is not None:
		footer += f' | 决定时间：{approval.decided_at}'
	if approval.reviewer_id is not None:
		footer += f' | 审批人：{approval.reviewer_id}'
	embed.set_footer(text=footer)
	return embed


class DecisionButton(discord.ui.DynamicItem[discord.ui.Button[discord.ui.View]], template=r'ac:(?P<approval_id>[1-9][0-9]*):(?P<decision>approved|rejected)'):
	def __init__(self, approval_id: int, decision: ApprovalStatus, disabled: bool = False):
		self.approval_id = approval_id
		self.decision = decision
		super().__init__(discord.ui.Button(
			label='同意' if decision == ApprovalStatus.APPROVED else '拒绝',
			style=discord.ButtonStyle.success if decision == ApprovalStatus.APPROVED else discord.ButtonStyle.danger,
			custom_id=f'ac:{approval_id}:{decision.value}', disabled=disabled,
		))

	@classmethod
	async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str]) -> 'DecisionButton':
		return cls(int(match['approval_id']), ApprovalStatus(match['decision']))

	async def callback(self, interaction: discord.Interaction) -> None:
		# Acknowledge before current-role lookup or database work.
		await interaction.response.defer(ephemeral=True, thinking=True)
		client = interaction.client
		if not isinstance(client, ApprovalBot):
			await interaction.followup.send('审批服务暂时不可用。', ephemeral=True)
			return
		await client.handle_decision(interaction, self.approval_id, self.decision)


def card_view(approval_id: int, status: ApprovalStatus) -> discord.ui.View:
	view = discord.ui.View(timeout=None)
	for decision in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
		view.add_item(DecisionButton(approval_id, decision, disabled=status != ApprovalStatus.PENDING))
	return view


class ApprovalBot(discord.Client):
	def __init__(self, config: DiscordConfig, service: ApprovalService):
		intents = discord.Intents.none()
		intents.guilds = True
		super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
		self.config = config
		self.service = service
		self._connected_once = False
		self._closing = False
		self._active_handlers: set[asyncio.Task[object]] = set()
		self.add_dynamic_items(DecisionButton)

	@property
	def ready(self) -> bool:
		return self.is_ready() and not self.is_closed()

	async def on_ready(self) -> None:
		LOGGER.info('Discord connected%s', ' again' if self._connected_once else '')
		self._connected_once = True

	async def on_disconnect(self) -> None:
		LOGGER.warning('Discord disconnected')

	async def reviewer_allowed(self, guild: discord.Guild, user_id: int) -> bool:
		# REST lookup avoids using stale cached member roles or enabling privileged intents.
		member = await guild.fetch_member(user_id)
		return str(member.id) in self.config.reviewer_ids or any(str(role.id) in self.config.reviewer_role_ids for role in member.roles)

	async def handle_decision(self, interaction: discord.Interaction, approval_id: int, decision: ApprovalStatus) -> None:
		if self._closing:
			return
		task = asyncio.current_task()
		if task is not None:
			self._active_handlers.add(task)
		try:
			await self._handle_decision(interaction, approval_id, decision)
		finally:
			if task is not None:
				self._active_handlers.discard(task)

	async def close(self) -> None:
		self._closing = True
		tasks = tuple(self._active_handlers)
		for task in tasks:
			task.cancel()
		if tasks:
			await asyncio.gather(*tasks, return_exceptions=True)
		await super().close()

	async def _handle_decision(self, interaction: discord.Interaction, approval_id: int, decision: ApprovalStatus) -> None:
		try:
			if interaction.guild is None or interaction.message is None or interaction.channel_id is None:
				await interaction.followup.send('此位置不支持审批。', ephemeral=True)
				return
			if str(interaction.guild.id) != self.config.guild_id:
				await interaction.followup.send('此服务器不支持审批。', ephemeral=True)
				return
			if not await self.reviewer_allowed(interaction.guild, interaction.user.id):
				await interaction.followup.send('你没有审批权限。', ephemeral=True)
				return
			result = await self.service.decide(
				approval_id, decision, str(interaction.user.id), str(interaction.guild.id),
				str(interaction.channel_id), str(interaction.message.id),
			)
			text = f'审批完成：{status_text(result.approval.status)}。' if result.accepted else f'该单据已结束：{status_text(result.approval.status)}。'
			await interaction.followup.send(text, ephemeral=True)
		except ApprovalError as error:
			text = '审批单不存在或已清理。' if error.status_code == 404 else '此卡片未关联到有效审批单。'
			await interaction.followup.send(text, ephemeral=True)
		except Exception:
			LOGGER.exception('Discord decision failed approval_id=%s reviewer_id=%s', approval_id, interaction.user.id)
			await interaction.followup.send('审批处理失败，请稍后重试。', ephemeral=True)

	async def sync(self, snapshot: MessageSnapshot, display_name: str) -> MessageSyncResult:
		channel = await self.fetch_channel(int(snapshot.link.channel_id))
		if not isinstance(channel, (discord.TextChannel, discord.Thread)) or str(channel.guild.id) != snapshot.link.guild_id:
			raise RuntimeError('Configured approval channel is not a text channel in the expected guild')
		embed = render_card(snapshot, display_name)
		view = card_view(snapshot.approval.approval_id, snapshot.approval.status)
		if snapshot.link.message_id is None:
			message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
			return MessageSyncResult(str(message.id))
		try:
			message = await channel.fetch_message(int(snapshot.link.message_id))
			await message.edit(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
		except discord.NotFound as error:
			if error.code != 10008:  # Only an unknown message ends synchronization.
				raise
			LOGGER.warning('Approval message missing approval_id=%s message_id=%s', snapshot.approval.approval_id, snapshot.link.message_id)
			return MessageSyncResult(snapshot.link.message_id, missing=True)
		return MessageSyncResult(snapshot.link.message_id)
