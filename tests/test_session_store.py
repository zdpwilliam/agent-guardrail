import pytest

from guardrail.clock import now_iso
from guardrail.models import EntityDelta, SessionState
from guardrail.protocols import SessionRecord
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteSessionStore(backend)
    await backend.close()


def _record(session_id: str, task_id: str | None = "t-1") -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        agent_id="pricing_agent",
        task_id=task_id,
        created_at=now_iso(),
        expires_at="2999-01-01T00:00:00+00:00",
    )


def _state(session_id: str, budget: float = 1.0) -> SessionState:
    return SessionState(
        session_id=session_id,
        entities={
            "product:p-1": EntityDelta(entity_key="product:p-1", price_delta_pct=-5.0,
                                       last_updated_at=now_iso())
        },
        risk_budget=budget,
    )


async def test_save_then_load_state(store):
    await store.save(_record("s-1"))
    await store.save_state("s-1", _state("s-1"), expected_version=0)
    state, version = await store.load_state("s-1")
    assert version == 1
    assert state.risk_budget == 1.0
    assert state.entities["product:p-1"].price_delta_pct == -5.0


async def test_load_state_without_saved_state_returns_fresh(store):
    await store.save(_record("s-1"))
    state, version = await store.load_state("s-1")
    assert version == 0
    assert state.risk_budget == 1.0
    assert state.entities == {}


async def test_load_state_missing_session_returns_none(store):
    assert await store.load_state("s-nope") is None


async def test_cas_success_increments_version(store):
    await store.save(_record("s-1"))
    await store.save_state("s-1", _state("s-1"), expected_version=0)
    ok = await store.save_state("s-1", _state("s-1", budget=0.9), expected_version=1)
    assert ok is True
    state, version = await store.load_state("s-1")
    assert version == 2
    assert state.risk_budget == 0.9


async def test_cas_stale_version_fails_and_leaves_data_untouched(store):
    await store.save(_record("s-1"))
    await store.save_state("s-1", _state("s-1"), expected_version=0)
    # 用过期版本 0 再写——必须失败，且已落盘的状态原样保留。
    ok = await store.save_state("s-1", _state("s-1", budget=0.5), expected_version=0)
    assert ok is False
    state, version = await store.load_state("s-1")
    assert version == 1
    assert state.risk_budget == 1.0


async def test_cas_on_missing_session_fails(store):
    ok = await store.save_state("s-nope", _state("s-nope"), expected_version=0)
    assert ok is False


async def test_find_by_task_returns_only_same_task(store):
    for sid, task in (("s-1", "t-1"), ("s-2", "t-1"), ("s-3", "t-2"), ("s-4", None)):
        await store.save(_record(sid, task_id=task))
        await store.save_state(sid, _state(sid, budget=0.5), expected_version=0)

    found = {rec.session_id: (rec, st) for rec, st in await store.find_by_task("t-1")}
    assert set(found) == {"s-1", "s-2"}
    assert found["s-1"][0].agent_id == "pricing_agent"
    assert found["s-1"][1].risk_budget == 0.5


async def test_find_by_task_skips_sessions_without_state(store):
    await store.save(_record("s-1"))
    await store.save_state("s-1", _state("s-1"), expected_version=0)
    await store.save(_record("s-2"))  # 同 task，但没有 save_state 过
    assert len(await store.find_by_task("t-1")) == 1


async def test_sweep_expired_still_works(store):
    await store.save(_record("s-dead"))
    dead = await store.load("s-dead")
    assert dead is not None
    dead.expires_at = "2000-01-01T00:00:00+00:00"
    await store.save(dead)
    await store.save_state("s-dead", _state("s-dead"), expected_version=0)
    assert await store.sweep_expired(now_iso()) == 1
    assert await store.load_state("s-dead") is None
