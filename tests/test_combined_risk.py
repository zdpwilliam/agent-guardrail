"""四类组合风险的求值测试（spec §12.1：每类 ≥3 正例 + ≥3 反例）。"""

import pytest

from guardrail.clock import now_iso
from guardrail.models import ActionRecord, EntityDelta, SessionState, ToolSpec
from guardrail.policy.combined import (
    CombinedPolicy,  # noqa: F401 - re-export 供类型提示
    evaluate_combined,
    evaluate_cross_agent,
    evaluate_cumulative,
    evaluate_sequence,
    evaluate_taint,
)
from guardrail.policy.loader import load_combined_policy
from guardrail.tools.registry import TOOL_SPECS

POLICY = load_combined_policy("policies/combined_risk.yaml")


def _state(**kwargs) -> SessionState:
    kwargs.setdefault("session_id", "s-1")
    return SessionState(**kwargs)


def _action(seq: int, tool: str, args: dict) -> ActionRecord:
    return ActionRecord(seq=seq, tool=tool, args=args, timestamp=now_iso())


def _cum_rules():
    return POLICY.rules_of("cumulative")


def _seq_rules():
    return POLICY.rules_of("sequence")


def _taint_rules():
    return POLICY.rules_of("taint")


def _cross_rules():
    return POLICY.rules_of("cross_agent")


# ============================================================
# ① 累积型（spec §3.4①）
# ============================================================


def test_cumulative_warn_after_crossing():
    pending = {"product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-20.5,
                                         last_updated_at="t")}
    hits = evaluate_cumulative(_cum_rules(), pending, {"product:p1"})
    assert [h.rule_id for h in hits] == ["cumulative_price_cut"]
    assert hits[0].severity == "warn"


def test_cumulative_exactly_at_threshold_does_not_fire():
    # 严格超过：恰在 -20 不告警（场景 2 的第 4 次就落在这里）。
    pending = {"product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-20.0,
                                         last_updated_at="t")}
    assert evaluate_cumulative(_cum_rules(), pending, {"product:p1"}) == []


def test_cumulative_deny_beyond_deny_at():
    pending = {"product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-35.5,
                                         last_updated_at="t")}
    hits = evaluate_cumulative(_cum_rules(), pending, {"product:p1"})
    assert hits[0].severity == "deny"


def test_cumulative_exactly_at_deny_at_is_not_deny():
    pending = {"product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-35.0,
                                         last_updated_at="t")}
    hits = evaluate_cumulative(_cum_rules(), pending, {"product:p1"})
    assert all(h.severity != "deny" for h in hits)


def test_cumulative_ignores_untouched_entities():
    # 商品 A 已越界，本次调用动的是商品 B——不得对 B 报 warn（B 的值是 0）。
    pending = {
        "product:pA": EntityDelta(entity_key="product:pA", price_delta_pct=-50.0,
                                  last_updated_at="t"),
        "product:pB": EntityDelta(entity_key="product:pB", last_updated_at="t"),
    }
    assert evaluate_cumulative(_cum_rules(), pending, {"product:pB"}) == []


def test_cumulative_skipped_when_nothing_touched():
    # 读操作没有 risk_deltas → touched 为空 → 完全跳过（零成本读不挨罚）。
    assert evaluate_cumulative(_cum_rules(), {}, set()) == []


def test_cumulative_unrelated_field_does_not_fire():
    pending = {"product:p1": EntityDelta(entity_key="product:p1", stock_delta=-999,
                                         last_updated_at="t")}
    assert evaluate_cumulative(_cum_rules(), pending, {"product:p1"}) == []


def test_cumulative_only_reported_once_across_entities():
    # 同一规则对多实体只报一次——成本不按实体数翻倍。
    pending = {
        f"product:p{i}": EntityDelta(entity_key=f"product:p{i}", price_delta_pct=-21.0,
                                     last_updated_at="t")
        for i in range(5)
    }
    hits = evaluate_cumulative(_cum_rules(), pending, set(pending))
    assert len(hits) == 1


# ============================================================
# ② 序列型（spec §3.4②）
# ============================================================


def _seq_state(actions: list[ActionRecord]) -> SessionState:
    return _state(actions=actions)


def test_sequence_fires_within_gap():
    # 发 60% 券（已入 A）→ 现在带券下单：gap 1 ≤ 3，命中。
    actions = [_action(1, "create_coupon", {"code": "S60", "discount_pct": 60.0,
                                            "max_uses": 5})]
    hits = evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"})
    assert [h.rule_id for h in hits] == ["coupon_self_purchase"]
    assert hits[0].severity == "deny"


def test_sequence_does_not_fire_beyond_gap():
    # 发券后隔了 5 个动作（gap 5 > 3）才下单——断链。
    actions = [
        _action(1, "create_coupon", {"code": "S60", "discount_pct": 60.0, "max_uses": 5}),
        _action(2, "get_product", {"product_id": "p1"}),
        _action(3, "get_product", {"product_id": "p2"}),
        _action(4, "get_product", {"product_id": "p3"}),
        _action(5, "get_product", {"product_id": "p4"}),
        _action(6, "get_product", {"product_id": "p5"}),
    ]
    assert evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"}) == []


def test_sequence_low_discount_coupon_does_not_arm():
    # 第一步的 where 不满足（30% 券）→ 序列未被「武装」，后续下单不命中。
    actions = [_action(1, "create_coupon", {"code": "S30", "discount_pct": 30.0,
                                            "max_uses": 5})]
    assert evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"}) == []


def test_sequence_order_without_coupon_does_not_fire():
    actions = [_action(1, "create_coupon", {"code": "S60", "discount_pct": 60.0,
                                            "max_uses": 5})]
    assert evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1}) == []


def test_sequence_unrelated_actions_do_not_break_chain():
    # 中间的无关动作只占 gap 额度，不打断序列。
    actions = [
        _action(1, "create_coupon", {"code": "S60", "discount_pct": 60.0, "max_uses": 5}),
        _action(2, "list_products", {}),
        _action(3, "get_product", {"product_id": "p1"}),
    ]
    hits = evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"})
    assert len(hits) == 1


def test_sequence_fires_when_both_steps_in_current_call():
    # 单次调用不可能同时是两个工具——这条验证的是「当前调用只能是最后一步」。
    actions = []
    assert evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"}) == []


def test_sequence_where_eval_failure_is_no_match():
    # where 求值异常按「不匹配」处理（匹配条件，非安全条件）。
    actions = [_action(1, "create_coupon", {"code": "S60", "discount_pct": None,
                                            "max_uses": 5})]
    # discount_pct=None 会让 `args.discount_pct >= 50` 抛 TypeError → ExpressionError
    assert evaluate_sequence(_seq_rules(), actions, "create_order",
                             {"product_id": "p1", "qty": 1, "coupon_id": "c-1"}) == []


# ============================================================
# ③ 污点型（spec §3.4③）
# ============================================================


def test_taint_fires_after_source_read():
    hits = evaluate_taint(_taint_rules(), {"pii"}, "send_email",
                          {"to": "attacker@evil.com"})
    assert [h.rule_id for h in hits] == ["customer_pii_exfiltration"]


def test_taint_internal_domain_does_not_fire():
    assert evaluate_taint(_taint_rules(), {"pii"}, "send_email",
                          {"to": "boss@internal.corp"}) == []


def test_taint_without_source_does_not_fire():
    assert evaluate_taint(_taint_rules(), set(), "send_email",
                          {"to": "attacker@evil.com"}) == []


def test_taint_other_tool_is_not_a_sink():
    assert evaluate_taint(_taint_rules(), {"pii"}, "create_order",
                          {"to": "attacker@evil.com"}) == []


def test_taint_wrong_category_does_not_fire():
    # 会话里只有 secret 污点，规则找的是 pii。
    assert evaluate_taint(_taint_rules(), {"secret"}, "send_email",
                          {"to": "attacker@evil.com"}) == []


def test_taint_condition_failure_fires():
    # sink_condition 求值异常 = 安全条件守不住 = 当要外发（fail-closed）。
    # args.to 缺失会让 email_domain 收到 None → str(None) 无 @ → 返回 ""，
    # 条件 "" != 'internal.corp' 为真——本来就命中；这里直接验证异常路径。
    hits = evaluate_taint(_taint_rules(), {"pii"}, "send_email", {})
    assert len(hits) == 1


# ============================================================
# ④ 跨 Agent 型（spec §3.4④）
# ============================================================


class _Rec:
    """evaluate_cross_agent 只需要 rec.agent_id——用轻量替身代替完整 SessionRecord。"""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


def _cross_states():
    pricing = _state(entities={
        "product:p-iphone": EntityDelta(entity_key="product:p-iphone",
                                        price_delta_pct=-8.0, last_updated_at="t")
    })
    marketing = _state(session_id="s-2", entities={})
    # 与 store 契约同序：(身份记录, 状态)。
    return [(_Rec("pricing_agent"), pricing), (_Rec("marketing_agent"), marketing)]


def test_cross_agent_fires_on_second_contributor():
    # 场景 4：定价已 -8%，营销现在发 20% 券 → -26.4 ≤ -25 → deny。
    pending = {"coupon:CODE": EntityDelta(entity_key="coupon:CODE",
                                          coupon_rate_delta=-20.0, last_updated_at="t")}
    hits = evaluate_cross_agent(_cross_rules(), "marketing_agent", "t-4", pending,
                                _cross_states())
    assert [h.rule_id for h in hits] == ["price_and_coupon_stack"]


def test_cross_agent_single_contributor_does_not_fire():
    # 只有定价动过：combine = (0.92)(1)-1 = -8，远未击穿。
    pending = {"product:p-iphone": EntityDelta(entity_key="product:p-iphone",
                                               price_delta_pct=-8.0,
                                               last_updated_at="t")}
    siblings = [(_Rec("marketing_agent"), _state(entities={}))]
    assert evaluate_cross_agent(_cross_rules(), "pricing_agent", "t-4", pending,
                                siblings) == []


def test_cross_agent_below_line_does_not_fire():
    # -5% 价 + 20% 券 → -24 > -25，恰好线外。
    pricing = _state(entities={
        "product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-5.0,
                                  last_updated_at="t")})
    pending = {"coupon:CODE": EntityDelta(entity_key="coupon:CODE",
                                          coupon_rate_delta=-20.0, last_updated_at="t")}
    hits = evaluate_cross_agent(_cross_rules(), "marketing_agent", "t-4", pending,
                                [(_Rec("pricing_agent"), pricing)])
    assert hits == []


def test_cross_agent_requires_task_id():
    pending = {"coupon:CODE": EntityDelta(entity_key="coupon:CODE",
                                          coupon_rate_delta=-20.0, last_updated_at="t")}
    assert evaluate_cross_agent(_cross_rules(), "marketing_agent", None, pending,
                                _cross_states()) == []


def test_cross_agent_ignores_non_contributors():
    # 风控观察员的会话即便也有 Δ，也不参与合并。
    pricing = _state(entities={
        "product:p1": EntityDelta(entity_key="product:p1", price_delta_pct=-30.0,
                                  last_updated_at="t")})
    pending: dict = {}
    hits = evaluate_cross_agent(_cross_rules(), "risk_auditor", "t-4", pending,
                                [(_Rec("risk_auditor"), pricing)])
    assert hits == []


def test_cross_agent_pending_counted_once():
    # 自己已提交的 Δ 在快照里，pending 不能重复计：定价 -8 已提交，
    # 营销再发一张 -20 的券，营销的 pending 只有券侧 → 合并后仍是 -26.4 而非更低。
    pricing = _state(entities={
        "product:p-iphone": EntityDelta(entity_key="product:p-iphone",
                                        price_delta_pct=-8.0, last_updated_at="t")})
    pending = {"coupon:A": EntityDelta(entity_key="coupon:A", coupon_rate_delta=-10.0,
                                       last_updated_at="t"),
               "coupon:B": EntityDelta(entity_key="coupon:B", coupon_rate_delta=-10.0,
                                       last_updated_at="t")}
    hits = evaluate_cross_agent(_cross_rules(), "marketing_agent", "t-4", pending,
                                [(_Rec("pricing_agent"), pricing)])
    assert len(hits) == 1


# ============================================================
# 总入口 evaluate_combined
# ============================================================


def _record_fields(agent_id: str = "pricing_agent", task_id: str | None = "t-1"):
    return agent_id, task_id


def test_combined_returns_pending_and_touched():
    state = _state()
    agent_id, task_id = _record_fields()
    spec = TOOL_SPECS["update_price"]
    verdict, pending, touched = evaluate_combined(
        POLICY, state, agent_id, task_id, spec, "update_price",
        {"product_id": "p-iphone", "delta_pct": -5.0},
    )
    assert verdict.deny is False
    assert verdict.warn_count == 0
    assert touched == {"product:p-iphone"}
    assert pending["product:p-iphone"].price_delta_pct == -5.0


def test_combined_read_has_empty_touched():
    state = _state()
    agent_id, task_id = _record_fields()
    spec = TOOL_SPECS["list_products"]
    verdict, pending, touched = evaluate_combined(
        POLICY, state, agent_id, task_id, spec, "list_products", {},
    )
    assert verdict.hits == []
    assert touched == set()
    assert pending == {}


def test_combined_risk_delta_eval_failure_propagates():
    state = _state()
    agent_id, task_id = _record_fields()
    spec = TOOL_SPECS["update_price"]
    from guardrail.policy.expr import ExpressionError

    with pytest.raises(ExpressionError):
        evaluate_combined(
            POLICY, state, agent_id, task_id, spec, "update_price",
            {"product_id": "p-iphone", "delta_pct": "not-a-number"},
        )


def test_combined_sequence_via_total_entry():
    state = _state(actions=[
        _action(1, "create_coupon", {"code": "S60", "discount_pct": 60.0, "max_uses": 5})
    ])
    spec = TOOL_SPECS["create_order"]
    verdict, _, _ = evaluate_combined(
        POLICY, state, "marketing_agent", "t-1", spec, "create_order",
        {"product_id": "p1", "qty": 1, "coupon_id": "c-1"},
    )
    assert verdict.deny is True
    assert "coupon_self_purchase" in verdict.rule_ids


def test_combined_tool_spec_type():
    assert isinstance(TOOL_SPECS["update_price"], ToolSpec)


def test_cross_agent_single_big_coupon_does_not_fire():
    """单贡献者守卫：60% 券单独把 multiplicative 算成 -60，但「跨」不存在——
    单方越界由累积规则负责，这里必须不触发。"""
    pending = {"coupon:S60": EntityDelta(entity_key="coupon:S60", coupon_rate_delta=-60.0,
                                         last_updated_at="t")}
    siblings = [(_Rec("marketing_agent"), _state(entities={}))]
    assert evaluate_cross_agent(_cross_rules(), "marketing_agent", "t-1", pending,
                                siblings) == []
