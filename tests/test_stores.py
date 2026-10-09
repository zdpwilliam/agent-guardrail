import pytest

from guardrail.clock import now_iso
from guardrail.protocols import SessionRecord
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore


@pytest.fixture
async def backend(tmp_path):
    b = SqliteBackend(str(tmp_path / "gateway.db"))
    await b.connect()
    yield b
    await b.close()


async def test_connect_creates_all_guardrail_tables(backend):
    rows = await backend.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert {"sessions", "provenance", "idempotency", "audit_log"} <= names


async def test_sessions_has_expires_at_column(backend):
    cols = await backend.fetchall("PRAGMA table_info(sessions)")
    assert "expires_at" in {c["name"] for c in cols}


async def test_audit_log_has_replay_column(backend):
    cols = await backend.fetchall("PRAGMA table_info(audit_log)")
    types = {c["name"]: c["type"] for c in cols}
    assert types["replay"] == "INTEGER"
    assert types["entry_hash"] == "TEXT"


async def test_provenance_primary_key_is_session_scoped(backend):
    import sqlite3

    await backend.execute(
        "INSERT INTO provenance (session_id, entity_type, entity_id, issued_at)"
        " VALUES ('s1', 'product', 'p1', ?)",
        (now_iso(),),
    )
    await backend.commit()
    # 同一会话重复登记是 INSERT OR IGNORE 的职责，裸 INSERT 必须报唯一约束冲突。
    with pytest.raises(sqlite3.IntegrityError):
        await backend.execute(
            "INSERT INTO provenance (session_id, entity_type, entity_id, issued_at)"
            " VALUES ('s1', 'product', 'p1', ?)",
            (now_iso(),),
        )


def _record(session_id: str = "s-1", expires_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        agent_id="ops_agent",
        task_id=None,
        created_at=now_iso(),
        expires_at=expires_at or now_iso(),
    )


async def test_session_store_roundtrip(backend):
    store = SqliteSessionStore(backend)
    await store.save(_record())
    loaded = await store.load("s-1")
    assert loaded is not None
    assert loaded.agent_id == "ops_agent"
    assert loaded.expires_at


async def test_session_store_load_missing_returns_none(backend):
    assert await SqliteSessionStore(backend).load("s-nope") is None


async def test_sweep_expired_removes_only_expired(backend):
    store = SqliteSessionStore(backend)
    await store.save(_record("s-live", expires_at="2999-01-01T00:00:00+00:00"))
    await store.save(_record("s-dead", expires_at="2000-01-01T00:00:00+00:00"))
    removed = await store.sweep_expired(now_iso())
    assert removed == 1
    assert await store.load("s-live") is not None
    assert await store.load("s-dead") is None


def test_now_iso_is_timezone_aware_utc():
    from datetime import datetime

    parsed = datetime.fromisoformat(now_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
