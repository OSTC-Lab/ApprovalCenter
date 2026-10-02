import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from approval_center.approval import Approval, ApprovalContent, ApprovalFilter, ApprovalStatus, MessageLink, MessageSnapshot

SCHEMA = '''
BEGIN;
CREATE TABLE IF NOT EXISTS approval (
	approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
	client_id TEXT NOT NULL,
	reference_key TEXT,
	content TEXT NOT NULL,
	status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'timed_out', 'cancelled')),
	created_at INTEGER NOT NULL,
	expires_at INTEGER NOT NULL,
	updated_at INTEGER NOT NULL,
	reviewer_id TEXT,
	decided_at INTEGER,
	CHECK (
		(status = 'pending' AND reviewer_id IS NULL AND decided_at IS NULL)
		OR (status IN ('approved', 'rejected') AND reviewer_id IS NOT NULL AND decided_at IS NOT NULL)
		OR (status = 'timed_out' AND reviewer_id IS NULL AND decided_at IS NOT NULL AND decided_at = expires_at)
		OR (status = 'cancelled' AND reviewer_id IS NULL AND decided_at IS NOT NULL)
	)
);
CREATE TABLE IF NOT EXISTS approval_data (
	approval_id INTEGER PRIMARY KEY REFERENCES approval(approval_id) ON DELETE CASCADE,
	data BLOB NOT NULL DEFAULT X'',
	version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0)
);
CREATE TABLE IF NOT EXISTS approval_discord (
	approval_id INTEGER PRIMARY KEY REFERENCES approval(approval_id) ON DELETE CASCADE,
	guild_id TEXT NOT NULL,
	channel_id TEXT NOT NULL,
	message_id TEXT,
	needs_message_sync INTEGER NOT NULL DEFAULT 1 CHECK (needs_message_sync IN (0, 1))
);
CREATE INDEX IF NOT EXISTS approval_client_created ON approval(client_id, created_at, approval_id);
CREATE INDEX IF NOT EXISTS approval_client_updated ON approval(client_id, updated_at, approval_id);
CREATE INDEX IF NOT EXISTS approval_status_expires ON approval(status, expires_at);
CREATE INDEX IF NOT EXISTS approval_expires ON approval(expires_at);
PRAGMA user_version = 1;
COMMIT;
'''

APPROVAL_SELECT = '''
SELECT a.*, d.data, d.version AS data_version
FROM approval AS a JOIN approval_data AS d USING (approval_id)
'''


def approval_from_row(row: aiosqlite.Row) -> Approval:
	return Approval(
		approval_id=row['approval_id'], client_id=row['client_id'], reference_key=row['reference_key'],
		content=ApprovalContent.model_validate_json(row['content']), status=row['status'],
		created_at=row['created_at'], expires_at=row['expires_at'], updated_at=row['updated_at'],
		reviewer_id=row['reviewer_id'], decided_at=row['decided_at'],
		data=row['data'], data_version=row['data_version'],
	)


class Transaction:
	def __init__(self, connection: aiosqlite.Connection):
		self.connection = connection

	async def _one(self, sql: str, parameters: tuple[object, ...] = ()) -> aiosqlite.Row | None:
		async with self.connection.execute(sql, parameters) as cursor:
			return await cursor.fetchone()

	async def get(self, approval_id: int) -> Approval | None:
		row = await self._one(APPROVAL_SELECT + ' WHERE a.approval_id = ?', (approval_id,))
		return approval_from_row(row) if row is not None else None

	async def get_link(self, approval_id: int) -> MessageLink | None:
		row = await self._one('SELECT * FROM approval_discord WHERE approval_id = ?', (approval_id,))
		if row is None:
			return None
		return MessageLink(
			approval_id=row['approval_id'], guild_id=row['guild_id'], channel_id=row['channel_id'],
			message_id=row['message_id'], needs_message_sync=bool(row['needs_message_sync']),
		)

	async def create(self, client_id: str, content: ApprovalContent, expires_at: int, data: bytes, guild_id: str, channel_id: str, now: int, *, reference_key: str | None = None) -> Approval:
		async with self.connection.execute(
			'INSERT INTO approval (client_id, reference_key, content, created_at, expires_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
			(client_id, reference_key, content.model_dump_json(), now, expires_at, now),
		) as cursor:
			approval_id = cursor.lastrowid
		if approval_id is None:
			raise RuntimeError('SQLite did not return an approval ID')
		await self.connection.execute('INSERT INTO approval_data (approval_id, data) VALUES (?, ?)', (approval_id, data))
		await self.connection.execute('INSERT INTO approval_discord (approval_id, guild_id, channel_id) VALUES (?, ?, ?)', (approval_id, guild_id, channel_id))
		return Approval(
			approval_id=approval_id, client_id=client_id, reference_key=reference_key, content=content, status=ApprovalStatus.PENDING,
			created_at=now, expires_at=expires_at, updated_at=now, reviewer_id=None, decided_at=None,
			data=data, data_version=0,
		)

	async def set_decision(self, approval_id: int, status: ApprovalStatus, reviewer_id: str | None, decided_at: int, now: int) -> None:
		await self.connection.execute(
			'UPDATE approval SET status = ?, reviewer_id = ?, decided_at = ?, updated_at = ? WHERE approval_id = ?',
			(status.value, reviewer_id, decided_at, now, approval_id),
		)
		await self.connection.execute('UPDATE approval_discord SET needs_message_sync = 1 WHERE approval_id = ?', (approval_id,))

	async def expire_due(self, now: int, client_id: str | None = None) -> int:
		sql = "SELECT approval_id, expires_at FROM approval WHERE status = 'pending' AND expires_at <= ?"
		parameters: list[object] = [now]
		if client_id is not None:
			sql += ' AND client_id = ?'
			parameters.append(client_id)
		async with self.connection.execute(sql, parameters) as cursor:
			rows = list(await cursor.fetchall())
		for row in rows:
			await self.set_decision(row['approval_id'], ApprovalStatus.TIMED_OUT, None, row['expires_at'], now)
		return len(rows)

	async def list_approvals(self, client_id: str | None, filters: ApprovalFilter, retention_cutoff: int) -> list[Approval]:
		clauses = ['a.expires_at > ?']
		parameters: list[object] = [retention_cutoff]

		def add(clause: str, value: object | None) -> None:
			if value is not None:
				clauses.append(clause)
				parameters.append(value)

		add('a.client_id = ?', client_id)
		add('a.reference_key = ?', filters.reference_key)
		add('a.status = ?', filters.status.value if filters.status is not None else None)
		add('a.created_at >= ?', filters.created_from)
		add('a.created_at < ?', filters.created_before)
		add('a.updated_at >= ?', filters.updated_from)
		add('a.updated_at < ?', filters.updated_before)
		sql = APPROVAL_SELECT + ' WHERE ' + ' AND '.join(clauses) + ' ORDER BY a.created_at DESC, a.approval_id DESC LIMIT ? OFFSET ?'
		parameters.extend((filters.limit, filters.offset))
		async with self.connection.execute(sql, parameters) as cursor:
			return [approval_from_row(row) for row in await cursor.fetchall()]

	async def replace_data(self, approval_id: int, data: bytes, version: int, now: int) -> None:
		await self.connection.execute('UPDATE approval_data SET data = ?, version = ? WHERE approval_id = ?', (data, version, approval_id))
		await self.connection.execute('UPDATE approval SET updated_at = ? WHERE approval_id = ?', (now, approval_id))

	async def pending_messages(self, retention_cutoff: int) -> list[MessageSnapshot]:
		sql = APPROVAL_SELECT + ''' JOIN approval_discord AS m USING (approval_id)
WHERE m.needs_message_sync = 1 AND a.expires_at > ? ORDER BY a.approval_id'''
		# Read the linked approvals and links within the same transaction.
		async with self.connection.execute(sql, (retention_cutoff,)) as cursor:
			rows = await cursor.fetchall()
		result: list[MessageSnapshot] = []
		for row in rows:
			approval = approval_from_row(row)
			link = await self.get_link(approval.approval_id)
			if link is not None:
				result.append(MessageSnapshot(approval, link))
		return result

	async def save_message(self, approval_id: int, message_id: str, needs_sync: bool) -> None:
		await self.connection.execute(
			'UPDATE approval_discord SET message_id = ?, needs_message_sync = ? WHERE approval_id = ?',
			(message_id, int(needs_sync), approval_id),
		)

	async def cleanup(self, retention_cutoff: int) -> int:
		async with self.connection.execute('DELETE FROM approval WHERE expires_at <= ?', (retention_cutoff,)) as cursor:
			return cursor.rowcount


class Storage:
	def __init__(self, connection: aiosqlite.Connection):
		self.connection = connection
		self._lock = asyncio.Lock()

	@classmethod
	async def open(cls, path: Path) -> 'Storage':
		path.parent.mkdir(parents=True, exist_ok=True)
		connection = await aiosqlite.connect(path, isolation_level=None)
		connection.row_factory = aiosqlite.Row
		try:
			await connection.execute('PRAGMA foreign_keys = ON')
			async with connection.execute('PRAGMA user_version') as cursor:
				row = await cursor.fetchone()
				if row is None or row[0] not in (0, 1):
					raise RuntimeError('Unsupported database schema version')
			await connection.executescript(SCHEMA)
		except BaseException:
			await connection.close()
			raise
		return cls(connection)

	@asynccontextmanager
	async def transaction(self) -> AsyncIterator[Transaction]:
		async with self._lock:
			try:
				await self.connection.execute('BEGIN')
				yield Transaction(self.connection)
				await self.connection.commit()
			except BaseException:
				await self.connection.rollback()
				raise

	async def healthy(self) -> bool:
		try:
			async with self.transaction() as transaction:
				return await transaction._one('SELECT 1') is not None
		except Exception:
			return False

	async def close(self) -> None:
		async with self._lock:
			await self.connection.close()
