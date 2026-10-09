"""旧开发库的列补齐迁移（CREATE TABLE IF NOT EXISTS 不给旧表加列）。"""

import aiosqlite

from guardrail.stores.plans import SqlitePlanStore
from guardrail.stores.sqlite import SqliteBackend


async def test_old_db_without_outputs_json_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    # 手工建一个「加 outputs_json 之前」的旧 plans 表。
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "CREATE TABLE plans ("
            " plan_id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT,"
            " intent TEXT, actions_json TEXT, status TEXT, risk_level TEXT,"
            " triggered_rules_json TEXT DEFAULT '[]', reasons_json TEXT DEFAULT '[]',"
            " projected_json TEXT, token_json TEXT, token_expires_at TEXT,"
            " executed_hashes_json TEXT DEFAULT '[]', created_at TEXT,"
            " decided_at TEXT, decided_by TEXT, version INTEGER DEFAULT 0)"
        )
        await conn.execute(
            "INSERT INTO plans (plan_id, session_id, agent_id, intent,"
            " actions_json, status, created_at)"
            " VALUES ('pl-old', 's-1', 'ops_agent', '', '[]', 'pending', 't')"
        )
        await conn.commit()

    backend = SqliteBackend(str(db))
    await backend.connect()
    try:
        store = SqlitePlanStore(backend)
        plan = await store.load("pl-old")
        assert plan is not None
        assert plan.outputs == {}  # 旧行读出新列默认值
        # 旧库上照常写入（含 outputs 记账）。
        assert await store.consume_hash("pl-old", "h" * 64, expected_version=0,
                                        outputs={"preview-coupon-0": "c-1"}) is True
        migrated = await store.load("pl-old")
        assert migrated.outputs == {"preview-coupon-0": "c-1"}
    finally:
        await backend.close()


async def test_fresh_db_unaffected(tmp_path):
    backend = SqliteBackend(str(tmp_path / "fresh.db"))
    await backend.connect()
    try:
        rows = await backend.fetchall("PRAGMA table_info(plans)")
        assert "outputs_json" in {r["name"] for r in rows}
        versions = [
            r["version"]
            for r in await backend.fetchall(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2]
    finally:
        await backend.close()


async def test_sqlite_uses_wal_and_busy_timeout(tmp_path):
    backend = SqliteBackend(str(tmp_path / "pragmas.db"), busy_timeout_ms=9000)
    await backend.connect()
    try:
        journal = await backend.fetchone("PRAGMA journal_mode")
        timeout = await backend.fetchone("PRAGMA busy_timeout")
        assert journal["journal_mode"].lower() == "wal"
        assert timeout["timeout"] == 9000
    finally:
        await backend.close()
