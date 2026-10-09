import asyncio

import pytest

from guardrail.clock import now_iso
from guardrail.models import SessionState
from guardrail.policy.loader import load_combined_policy
from guardrail.session import VersionConflictError, authorize, commit_effect
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore

POLICY = load_combined_policy("policies/combined_risk.yaml")


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteSessionStore(backend)
    await backend.close()


async def _mk_session(store, session_id: str, agent_id: str = "pricing_agent",
                      task_id: str | None = "t-1") -> None:
    from guardrail.protocols import SessionRecord

    await store.save(SessionRecord(
        session_id=session_id, agent_id=agent_id, task_id=task_id,
        created_at=now_iso(), expires_at="2999-01-01T00:00:00+00:00",
    ))


# ---------- authorize ----------


async def test_authorize_allows_first_price_cut(store):
    await _mk_session(store, "s-1")
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    assert auth.decision == "allow"
    assert auth.cost == 0.15
    assert auth.version == 0
    # 决策读扣减前预算——预算在快照里是 1.0（扣减发生在提交段）。
    state, _ = await store.load_state("s-1")
    assert state.risk_budget == 1.0


async def test_authorize_no_state_write_on_allow(store):
    # 只记生效调用：授权段不落盘。
    await _mk_session(store, "s-1")
    await authorize(store, POLICY, "s-1", "update_price",
                    {"product_id": "p-1", "delta_pct": -5.0})
    _, version = await store.load_state("s-1")
    assert version == 0


async def test_authorize_deny_on_cumulative_breach(store):
    await _mk_session(store, "s-1")
    from guardrail.models import EntityDelta

    state = SessionState(
        session_id="s-1",
        entities={"product:p-1": EntityDelta(entity_key="product:p-1",
                                             price_delta_pct=-33.0,
                                             last_updated_at=now_iso())},
    )
    await store.save_state("s-1", state, expected_version=0)
    # 会话里已累计 -33，本次再 -5 → pending -38，越过 deny_at -35。

    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    assert auth.decision == "deny"
    assert "cumulative_price_cut" in auth.rule_ids


async def test_authorize_ask_when_budget_low(store):
    await _mk_session(store, "s-1")
    state = SessionState(session_id="s-1", risk_budget=0.05)
    await store.save_state("s-1", state, expected_version=0)
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    assert auth.decision == "ask"
    # pending 累计只有 -5，没越过 warn_at -20 → 无附加，cost = 0.15。
    assert auth.cost == 0.15


async def test_authorize_allow_with_flag_in_ladder(store):
    await _mk_session(store, "s-1")
    state = SessionState(session_id="s-1", risk_budget=0.25)
    await store.save_state("s-1", state, expected_version=0)
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    assert auth.decision == "allow_with_flag"


async def test_authorize_version_conflict_after_two_attempts(store):
    await _mk_session(store, "s-1")
    original_load = store.load_state

    async def racing_load(session_id):
        # 每次授权读快照后，都立刻有「另一个请求」抢写版本——
        # 授权段的版本双检两次都失配 → VersionConflictError（spec §3.7.3：
        # 二次失配必须 409，绝不尽力而为地放行）。
        st, v = await original_load(session_id)
        await store.save_state(session_id, st, expected_version=v)
        return st, v

    store.load_state = racing_load  # type: ignore[method-assign]

    with pytest.raises(VersionConflictError):
        await authorize(store, POLICY, "s-1", "update_price",
                        {"product_id": "p-1", "delta_pct": -5.0})


# ---------- commit_effect ----------


async def test_commit_effect_applies_delta_taint_action_budget(store):
    await _mk_session(store, "s-1", agent_id="ops_agent")
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    assert auth.decision == "allow"
    from guardrail.tools.registry import TOOL_SPECS

    ok = await commit_effect(store, "s-1", auth, TOOL_SPECS["update_price"],
                             {"product_id": "p-1", "delta_pct": -5.0}, auth.cost)
    assert ok is True
    state, version = await store.load_state("s-1")
    assert version == 1
    assert state.risk_budget == pytest.approx(0.85)
    assert state.entities["product:p-1"].price_delta_pct == -5.0
    assert state.actions[0].tool == "update_price"


async def test_commit_effect_records_taint_for_reads(store):
    from guardrail.tools.registry import TOOL_SPECS

    await _mk_session(store, "s-1", agent_id="ops_agent")
    auth = await authorize(store, POLICY, "s-1", "get_order", {"order_id": "o-1"})
    assert auth.decision == "allow"
    await commit_effect(store, "s-1", auth, TOOL_SPECS["get_order"],
                        {"order_id": "o-1"}, auth.cost)
    state, _ = await store.load_state("s-1")
    assert state.taint == {"pii"}


async def test_commit_effect_caps_actions_at_200(store):
    from guardrail.tools.registry import TOOL_SPECS

    await _mk_session(store, "s-1", agent_id="ops_agent")
    for _i in range(205):
        auth = await authorize(store, POLICY, "s-1", "get_product",
                               {"product_id": "p-1"})
        await commit_effect(store, "s-1", auth, TOOL_SPECS["get_product"],
                            {"product_id": "p-1"}, 0.0)
    state, _ = await store.load_state("s-1")
    assert len(state.actions) == 200
    assert state.actions[-1].seq == 205


async def test_commit_effect_cas_conflict_reapplies_on_fresh_state(store):
    from guardrail.tools.registry import TOOL_SPECS

    await _mk_session(store, "s-1", agent_id="ops_agent")
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})
    # 授权之后、提交之前，另一个请求抢先提交了一次（版本 +1）。
    st, v = await store.load_state("s-1")
    st.entities["product:p-other"] = _other_entity()
    await store.save_state("s-1", st, expected_version=v)

    ok = await commit_effect(store, "s-1", auth, TOOL_SPECS["update_price"],
                             {"product_id": "p-1", "delta_pct": -5.0}, auth.cost)
    assert ok is True
    state, version = await store.load_state("s-1")
    # 抢先者的增量不丢，本调用的增量也补上（重载重放）。
    assert "product:p-other" in state.entities
    assert state.entities["product:p-1"].price_delta_pct == -5.0
    assert state.risk_budget == pytest.approx(0.85)


def _other_entity():
    from guardrail.models import EntityDelta

    return EntityDelta(entity_key="product:p-other", last_updated_at=now_iso())


async def test_commit_effect_double_conflict_returns_false(store):
    from guardrail.tools.registry import TOOL_SPECS

    await _mk_session(store, "s-1", agent_id="ops_agent")
    auth = await authorize(store, POLICY, "s-1", "update_price",
                           {"product_id": "p-1", "delta_pct": -5.0})

    original = store.save_state

    async def always_conflict(session_id, state, expected_version):
        # 别人持续抢写：两次重放都失配。
        st, v = await store.load_state(session_id)
        await original(session_id, st, expected_version=v)
        return await original(session_id, state, expected_version)

    store.save_state = always_conflict  # type: ignore[method-assign]
    ok = await commit_effect(store, "s-1", auth, TOOL_SPECS["update_price"],
                             {"product_id": "p-1", "delta_pct": -5.0}, auth.cost)
    assert ok is False


# ---------- 并发（spec §12.1 点名） ----------


async def test_concurrent_commits_both_land_and_budget_sums(store):
    from guardrail.tools.registry import TOOL_SPECS

    await _mk_session(store, "s-1", agent_id="ops_agent")

    async def one_call():
        auth = await authorize(store, POLICY, "s-1", "update_price",
                               {"product_id": "p-1", "delta_pct": -5.0})
        if auth.decision != "allow":
            return None
        return await commit_effect(store, "s-1", auth, TOOL_SPECS["update_price"],
                                   {"product_id": "p-1", "delta_pct": -5.0},
                                   auth.cost)

    results = await asyncio.gather(one_call(), one_call())
    assert results == [True, True]
    state, version = await store.load_state("s-1")
    # 两次各扣 0.15，版本 +2：CAS 重放保证两次增量都不丢。
    assert state.risk_budget == pytest.approx(0.70)
    assert version == 2
