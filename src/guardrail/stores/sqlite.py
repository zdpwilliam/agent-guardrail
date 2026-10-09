from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from guardrail.clock import now_iso
from guardrail.models import SessionState
from guardrail.protocols import SessionRecord

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  -- M3：单一 state_json + version 乐观锁（spec §3.7）。风险预算在 state 里，
  -- 不设独立列——两处真相必须同事务双写，是 bug 温床（偏离 §15，计划已注明）。
  state_json TEXT,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS provenance (
  session_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  PRIMARY KEY (session_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- in_progress | done
  response_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
  plan_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  intent TEXT NOT NULL DEFAULT '',
  actions_json TEXT NOT NULL,
  status TEXT NOT NULL,
  risk_level TEXT,
  triggered_rules_json TEXT NOT NULL DEFAULT '[]',
  reasons_json TEXT NOT NULL DEFAULT '[]',
  projected_json TEXT,
  token_json TEXT,
  token_expires_at TEXT,
  executed_hashes_json TEXT NOT NULL DEFAULT '[]',
  outputs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  -- 乐观锁：消费记账的 CAS 面（同 §3.7 立场，单进程 asyncio 也需要）
  version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);

CREATE TABLE IF NOT EXISTS pending_approvals (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  reasons_json TEXT NOT NULL,
  cost REAL NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT,
  decided_by TEXT,
  comment TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_open ON pending_approvals(resolved_at);

CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL,
  session_id TEXT NOT NULL,
  plan_id TEXT,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  decision TEXT NOT NULL,          -- allow | allow_with_flag | ask | deny
  replay INTEGER NOT NULL DEFAULT 0,
  reasons_json TEXT NOT NULL,
  timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
"""


class SqliteBackend:
    """网关侧 SQLite 的共享连接。

    四个存储（会话 / 审计 / 幂等 / provenance）共用一个连接，而不是各开一个：
    多个连接写同一个文件会撞上 SQLite 的写锁，而单进程下没有理由付这个代价。
    多进程扩展时这一步会变成连接池，调用方（各 Store）代码不变。
    """

    def __init__(self, db_path: str, *, busy_timeout_ms: int = 5000) -> None:
        self.db_path = db_path
        self.busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(
            f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}"
        )
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """版本化迁移。

        当前数据库只有一个生产版本，因此迁移 1 是基线，迁移 2 只做旧 plans
        表的 `outputs_json` 补列。后续 schema 变更必须继续追加版本，不再靠
        删库重建。迁移只向前执行；恢复依赖部署前的 SQLite 备份。
        """
        applied = {
            row["version"]
            for row in await self.backend_fetchall("SELECT version FROM schema_migrations")
        }
        if 1 not in applied:
            await self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (1, now_iso()),
            )
        if 2 not in applied:
            # 旧库可能缺少这个列；新库在 SCHEMA 中已经创建。
            cur = await self._conn.execute("PRAGMA table_info(plans)")
            existing = {r["name"] for r in await cur.fetchall()}
            if "outputs_json" not in existing:
                await self._conn.execute(
                    "ALTER TABLE plans ADD COLUMN outputs_json"
                    " TEXT NOT NULL DEFAULT '{}'"
                )
            await self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (2, now_iso()),
            )

    async def backend_fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        """迁移阶段读取辅助，避免调用尚未初始化完成的公共 fetchall。"""
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteBackend.connect() 未调用")
        return self._conn

    async def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(
        self, sql: str, params: list[tuple[Any, ...]]
    ) -> aiosqlite.Cursor:
        return await self.conn.executemany(sql, params)

    async def commit(self) -> None:
        await self.conn.commit()


class SqliteSessionStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def load(self, session_id: str) -> SessionRecord | None:
        row = await self.backend.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return SessionRecord(
            session_id=row["id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def save(self, record: SessionRecord) -> None:
        await self.backend.execute(
            "INSERT OR REPLACE INTO sessions (id, agent_id, task_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                record.session_id,
                record.agent_id,
                record.task_id,
                record.created_at,
                record.expires_at,
            ),
        )
        await self.backend.commit()

    async def sweep_expired(self, now_iso: str) -> int:
        """删掉已过期会话。

        ISO 8601 带同一时区偏移的文本字典序即时间序，所以字符串比较就够了——
        前提是所有写入都带时区（`clock.now_iso()` 保证这一点）。
        """
        cur = await self.backend.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        await self.backend.commit()
        return cur.rowcount if cur.rowcount is not None else 0


    # ---------- 风险状态（spec §3.6 三元组的持久化） ----------

    async def load_state(self, session_id: str) -> tuple[SessionState, int] | None:
        row = await self.backend.fetchone(
            "SELECT state_json, version FROM sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return None
        if row["state_json"] is None:
            # 新会话还没写过状态：返回全新状态 + 版本 0，省掉建会话时的一次写。
            return SessionState(session_id=session_id), 0
        return SessionState.model_validate(json.loads(row["state_json"])), row["version"]

    async def save_state(
        self, session_id: str, state: SessionState, expected_version: int
    ) -> bool:
        """CAS 写入（spec §3.7）。失配返回 False，绝不覆盖。"""
        cur = await self.backend.execute(
            "UPDATE sessions SET state_json = ?, version = version + 1"
            " WHERE id = ? AND version = ?",
            (state.model_dump_json(), session_id, expected_version),
        )
        await self.backend.commit()
        return (cur.rowcount or 0) > 0

    async def find_by_task(
        self, task_id: str
    ) -> list[tuple[SessionRecord, SessionState]]:
        """跨 Agent 合并的唯一入口（spec §19.1）。只返回已写过状态的会话。

        返回 (身份记录, 状态) 对：contributors 过滤需要 agent_id，而
        SessionState 刻意不携带身份字段（单一事实来源）。
        """
        rows = await self.backend.fetchall(
            "SELECT * FROM sessions WHERE task_id = ? AND state_json IS NOT NULL",
            (task_id,),
        )
        return [
            (
                SessionRecord(
                    session_id=r["id"], agent_id=r["agent_id"], task_id=r["task_id"],
                    created_at=r["created_at"], expires_at=r["expires_at"],
                ),
                SessionState.model_validate(json.loads(r["state_json"])),
            )
            for r in rows
        ]
