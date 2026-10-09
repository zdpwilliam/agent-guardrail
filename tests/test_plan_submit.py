"""计划提交链测试（spec §4.4：rejected / high / medium / low 四级判定）。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.clock import now_iso
from guardrail.models import SessionState
from guardrail.plans import (
    PlanDecision,  # noqa: F401
    PlannedAction,
    action_hash,
)
from guardrail.policy.loader import load_combined_policy, load_policy
from guardrail.protocols import SessionRecord
from guardrail.stores.plans import SqlitePlanStore
from guardrail.stores.provenance import SqliteProvenanceStore
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore
from shop.main import create_app as create_shop_app

COMBINED = load_combined_policy("policies/combined_risk.yaml")
SINGLE = load_policy("policies/single_call.yaml")

PLAN_POLICY = type("PP", (), {
    "token_ttl_minutes": 15,
    "low_budget_floor": 0.30,
    "high_warn_count": 2,
})()


@pytest.fixture
async def env(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        transport = httpx.ASGITransport(app=shop_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://shop.test") as shop:
            backend = SqliteBackend(str(tmp_path / "gateway.db"))
            await backend.connect()
            sessions = SqliteSessionStore(backend)
            plans = SqlitePlanStore(backend)
            provenance = SqliteProvenanceStore(backend)
            record = SessionRecord(
                session_id="s-1", agent_id="ops_agent", task_id="t-1",
                created_at=now_iso(), expires_at="2999-01-01T00:00:00+00:00",
            )
            await sessions.save(record)
            yield {"shop": shop, "sessions": sessions, "plans": plans,
                   "provenance": provenance, "record": record, "backend": backend}
            await backend.close()


async def _state(env, budget: float = 1.0) -> SessionState:
    state = SessionState(session_id="s-1", risk_budget=budget)
    await env["sessions"].save_state("s-1", state, expected_version=0)
    return state


async def _submit(env, state, actions, intent="清仓", agent_id="ops_agent"):
    from guardrail.plans import submit_plan

    env["record"].agent_id = agent_id
    return await submit_plan(
        plan_store=env["plans"], session_store=env["sessions"],
        provenance_store=env["provenance"],
        single_policy=SINGLE, combined=COMBINED, plan_policy=PLAN_POLICY,
        record=env["record"], state=state, intent=intent,
        actions=actions, shop=env["shop"],
    )


# ---------- rejected：语法层预检 ----------


async def test_rejected_on_schema_violation(env):
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price", args={"product_id": "p-iphone"}),
    ])
    assert decision.risk_level == "rejected"
    assert plan.status == "rejected"
    assert any("delta_pct" in r for r in decision.reasons)


async def test_rejected_on_single_threshold(env):
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -50.0}),
    ])
    assert decision.risk_level == "rejected"
    assert "max_single_price_cut" in decision.reasons[0]


async def test_rejected_on_unknown_tool(env):
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="teleport", args={}),
    ])
    assert decision.risk_level == "rejected"


# ---------- high / medium：计划级组合风险（终态语义） ----------


async def test_high_on_cumulative_deny_across_plan(env):
    # 5 次 -8%：单次全部合规（≤10%），计划终态累计 -40 越过 deny_at -35。
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=i, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -8.0})
        for i in range(5)
    ])
    assert decision.risk_level == "high"
    assert "cumulative_price_cut" in decision.triggered_rules
    assert plan.status == "pending"


async def test_medium_on_single_warn(env):
    # 3 次 -8%：终态 -24 越过 warn_at -20、未到 deny_at -35 → medium。
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=i, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -8.0})
        for i in range(3)
    ])
    assert decision.risk_level == "medium"
    assert "cumulative_price_cut" in decision.triggered_rules


async def test_high_on_multiple_warns(env):
    # 「多条 warn → high」按**规则**计数（M3 语义：同一规则对多实体只报一次，
    # 否则 30 商品清仓会翻倍计费）。两条不同规则各命中一次 → high。
    from guardrail.policy.combined import (
        BudgetConfig,
        CombinedPolicy,
        CostConfig,
        CumulativeRule,
        ThresholdConfig,
    )

    two_warn = CombinedPolicy(
        version=1,
        budget=BudgetConfig(
            initial=1.0,
            costs=CostConfig(
                sensitive_write=0.15, normal_write=0.05, warn_surcharge=0.20,
                cost_classes={"sensitive_write": ["update_price"],
                              "normal_write": ["update_stock"],
                              "read": ["list_products", "get_product", "get_order",
                                       "create_coupon", "create_order",
                                       "refund_order", "send_email"]},
            ),
            thresholds=ThresholdConfig(deny_below=0.0, ask_at_or_below=0.10,
                                       flag_at_or_below=0.30),
        ),
        rules=[
            CumulativeRule(type="cumulative", id="w_price", severity="warn",
                           field="price_delta_pct", warn_at=-20.0, message="m1"),
            CumulativeRule(type="cumulative", id="w_coupon", severity="warn",
                           field="coupon_rate_delta", warn_at=-10.0, message="m2"),
        ],
    )

    state = await _state(env)
    from guardrail.plans import submit_plan

    env["record"].agent_id = "ops_agent"
    plan, decision = await submit_plan(
        plan_store=env["plans"], session_store=env["sessions"],
        provenance_store=env["provenance"], single_policy=SINGLE,
        combined=two_warn, plan_policy=PLAN_POLICY,
        record=env["record"], state=state, intent="清仓",
        actions=[
            PlannedAction(step=0, tool="update_price",
                          args={"product_id": "p-iphone", "delta_pct": -8.0}),
            PlannedAction(step=1, tool="update_price",
                          args={"product_id": "p-iphone", "delta_pct": -8.0}),
            PlannedAction(step=2, tool="update_price",
                          args={"product_id": "p-iphone", "delta_pct": -8.0}),
            PlannedAction(step=3, tool="create_coupon",
                          args={"code": "S20", "discount_pct": 20.0, "max_uses": 5}),
        ],
        shop=env["shop"],
    )
    assert decision.risk_level == "high"
    assert set(decision.triggered_rules) == {"w_price", "w_coupon"}


async def test_high_on_sequence_rule(env):
    # 计划内：发 60% 券 → 用券下单（preview 占位 id 由投影合成）→ 序列 deny。
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="create_coupon",
                      args={"code": "S60", "discount_pct": 60.0, "max_uses": 5}),
        PlannedAction(step=1, tool="create_order",
                      args={"product_id": "p-tshirt-s", "qty": 1,
                            "coupon_id": "preview-coupon-0"}),
    ])
    assert decision.risk_level == "high"
    assert "coupon_self_purchase" in decision.triggered_rules


async def test_high_on_cross_agent_stack(env):
    # 定价会话已提交 -8%；计划的 -8% × 兄弟会话已有 20% 券 → 击穿。
    # 这里用双贡献者构造：当前会话 plan 贡献 price，兄弟贡献 coupon。
    state = await _state(env)
    from guardrail.models import EntityDelta

    sibling_state = SessionState(session_id="s-2", entities={
        "coupon:C20": EntityDelta(entity_key="coupon:C20", coupon_rate_delta=-20.0,
                                  last_updated_at=now_iso())
    })
    # find_by_task 按 task_id 找——s-1 与 s-2 同 task；直接把兄弟状态种进库。
    from guardrail.protocols import SessionRecord

    await env["sessions"].save(SessionRecord(
        session_id="s-2", agent_id="marketing_agent", task_id="t-1",
        created_at=now_iso(), expires_at="2999-01-01T00:00:00+00:00",
    ))
    await env["sessions"].save_state("s-2", sibling_state, expected_version=0)

    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -8.0}),
    ], agent_id="pricing_agent")
    assert decision.risk_level == "high"
    assert "price_and_coupon_stack" in decision.triggered_rules


# ---------- low：自动批准 ----------


async def test_low_auto_approves_with_token(env):
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -3.0}),
    ])
    assert decision.risk_level == "low"
    assert plan.status == "approved"
    assert decision.token is not None
    assert decision.token.action_hashes == [action_hash("update_price",
                                                        {"product_id": "p-iphone",
                                                         "delta_pct": -3.0})]
    loaded = await env["plans"].load(plan.plan_id)
    assert loaded.token() is not None
    assert loaded.token_expires_at is not None


async def test_low_budget_promotes_to_medium(env):
    state = await _state(env, budget=0.20)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -3.0}),
    ])
    assert decision.risk_level == "medium"
    assert plan.status == "pending"


# ---------- 投影与 metrics ----------


async def test_projection_state_is_saved(env):
    state = await _state(env)
    plan, decision = await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -3.0}),
    ])
    assert plan.projected_json is not None
    assert "p-iphone" in plan.projected_json


async def test_plan_delta_on_session_copy_not_committed(env):
    # 提交计划不改会话状态——Δ 只在副本上叠加，执行时才生效。
    state = await _state(env)
    await _submit(env, state, [
        PlannedAction(step=0, tool="update_price",
                      args={"product_id": "p-iphone", "delta_pct": -3.0}),
    ])
    current, _ = await env["sessions"].load_state("s-1")
    assert current.entities == {}
    assert current.risk_budget == 1.0
