from datetime import UTC

import pytest

from guardrail.clock import now_iso
from guardrail.plans import Plan, PlannedAction
from guardrail.stores.plans import SqlitePlanStore
from guardrail.stores.sqlite import SqliteBackend


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqlitePlanStore(backend)
    await backend.close()


def _plan(plan_id: str = "pl-1", status: str = "pending", **kwargs) -> Plan:
    base = dict(
        plan_id=plan_id,
        session_id="s-1",
        agent_id="pricing_agent",
        intent="清仓夏季款",
        actions=[
            PlannedAction(step=0, tool="update_price",
                          args={"product_id": "p-1", "delta_pct": -5.0}),
            PlannedAction(step=1, tool="update_price",
                          args={"product_id": "p-2", "delta_pct": -5.0}),
        ],
        status=status,
        created_at=now_iso(),
    )
    return Plan(**{**base, **kwargs})


# ---------- 基础 CRUD ----------


async def test_create_and_load(store):
    await store.create(_plan())
    loaded = await store.load("pl-1")
    assert loaded is not None
    assert loaded.intent == "清仓夏季款"
    assert [a.step for a in loaded.actions] == [0, 1]
    assert loaded.status == "pending"


async def test_load_missing_returns_none(store):
    assert await store.load("pl-nope") is None


async def test_list_by_status(store):
    await store.create(_plan("pl-1", status="pending"))
    await store.create(_plan("pl-2", status="approved"))
    await store.create(_plan("pl-3", status="pending"))
    ids = [p.plan_id for p in await store.list_by_status("pending")]
    assert ids == ["pl-1", "pl-3"]


async def test_update_status_requires_legal_transition(store):
    await store.create(_plan())
    # pending → approved 合法
    assert await store.update_status("pl-1", "approved") is True
    # pending → completed 非法（必须经 approved）
    await store.create(_plan("pl-2"))
    with pytest.raises(Exception, match="非法"):
        await store.update_status("pl-2", "completed")


# ---------- CAS 消费 ----------


async def test_consume_hash_appends_atomically(store):
    import asyncio

    await store.create(_plan("pl-1", status="approved"))
    h0 = "a" * 64
    h1 = "b" * 64
    results = await asyncio.gather(
        store.consume_hash("pl-1", h0, expected_version=0),
        store.consume_hash("pl-1", h1, expected_version=0),
    )
    assert results.count(True) == 1  # 并发消费同一版本，恰一成功
    plan = await store.load("pl-1")
    assert len(plan.executed_hashes) == 1


async def test_consume_hash_records_executed_step(store):
    await store.create(_plan("pl-1", status="approved"))
    assert await store.consume_hash("pl-1", "c" * 64, expected_version=0) is True
    plan = await store.load("pl-1")
    assert plan.executed_hashes == ["c" * 64]


# ---------- 惰性过期 ----------


async def test_expire_if_due_marks_expired(store):
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await store.create(_plan("pl-1", status="approved", token_expires_at=exp))
    assert await store.expire_if_due("pl-1") is True
    assert (await store.load("pl-1")).status == "expired"


async def test_expire_if_due_ignores_future_expiry(store):
    from datetime import datetime, timedelta

    exp = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    await store.create(_plan("pl-1", status="approved", token_expires_at=exp))
    assert await store.expire_if_due("pl-1") is False
    assert (await store.load("pl-1")).status == "approved"


async def test_pending_plan_never_expires(store):
    await store.create(_plan("pl-1", status="pending"))
    assert await store.expire_if_due("pl-1") is False


# ---------- 模型 ----------


def test_plan_token_shape():
    from guardrail.plans import PlanToken

    tok = PlanToken(plan_id="pl-1", session_id="s-1",
                    action_hashes=["a" * 64], exp="2026-10-08T10:00:00+00:00")
    assert tok.plan_id == "pl-1"
    assert tok.exp


def test_action_hash_is_stable_and_order_sensitive():
    from guardrail.plans import action_hash

    a = action_hash("update_price", {"product_id": "p-1", "delta_pct": -5.0})
    b = action_hash("update_price", dict(reversed(list({"product_id": "p-1",
                                                         "delta_pct": -5.0}.items()))))
    assert a == b  # 键序无关
    assert a != action_hash("update_stock", {"product_id": "p-1", "delta_pct": -5.0})
    assert a != action_hash("update_price", {"product_id": "p-2", "delta_pct": -5.0})


def test_action_hash_has_no_delimiter_ambiguity():
    from guardrail.plans import action_hash

    # spec §4.5 的 tool ‖ json 拼接有分隔符歧义；整体 canonical_json 消掉它
    # ——与 M2 幂等键同一立场。
    assert action_hash("t", {"a": 1}) != action_hash("t", {"a": 11})
    assert action_hash("t", {"a": 1}) != action_hash("ta", {"": 1})
