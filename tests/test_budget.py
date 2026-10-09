import pytest

from guardrail.policy.budget import classify_cost, synth_decision
from guardrail.policy.loader import load_combined_policy

POLICY = load_combined_policy("policies/combined_risk.yaml")
CFG = POLICY.budget


# ---------- 成本分类 ----------


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("update_price", 0.15),
        ("create_coupon", 0.15),
        ("update_stock", 0.05),
        ("create_order", 0.05),
        ("refund_order", 0.05),
        ("send_email", 0.05),
        ("list_products", 0.0),
        ("get_product", 0.0),
        ("get_order", 0.0),
    ],
)
def test_classify_cost_covers_all_tools(tool, expected):
    assert classify_cost(POLICY, tool) == expected


def test_classify_cost_unknown_tool_raises():
    # lint 已保证分类全覆盖；这里防的是「新增工具后忘了改策略」直接静默放行。
    with pytest.raises(RuntimeError, match="teleport"):
        classify_cost(POLICY, "teleport")


def test_cost_is_taken_from_config_not_hardcoded():
    # 改配置必须生效——§18.3 要求全部系数可调可测。
    import copy

    p2 = copy.deepcopy(POLICY)
    p2.budget.costs.sensitive_write = 0.5
    assert classify_cost(p2, "update_price") == 0.5


# ---------- 决策阶梯（扣减前判定） ----------


def test_ladder_allow():
    decision, cost = synth_decision(1.0, 0.15, 0, CFG)
    assert decision == "allow"
    assert cost == 0.15


def test_ladder_allow_with_flag_at_threshold():
    # 恰在 0.30 → flag（≤ 语义）。
    decision, cost = synth_decision(0.30, 0.15, 0, CFG)
    assert decision == "allow_with_flag"


def test_ladder_ask():
    # 场景 2 的第 6 次：预算 0.05 ≤ 0.10 → ASK。敏感写 0.15 + 1 次 warn 附加
    # = 0.35；cost 原样返回（批准后执行时才扣）。
    decision, cost = synth_decision(0.05, 0.15, 1, CFG)
    assert decision == "ask"
    assert cost == 0.35


def test_ladder_deny_overdraft():
    decision, _ = synth_decision(-0.01, 0.15, 0, CFG)
    assert decision == "deny"


def test_ladder_exact_zero_is_ask_not_deny():
    # deny 是严格 <；恰为 0 落在 ask 段。
    decision, _ = synth_decision(0.0, 0.15, 0, CFG)
    assert decision == "ask"


def test_ladder_boundaries_are_inclusive_for_ask_and_flag():
    decision, _ = synth_decision(0.10, 0.15, 0, CFG)
    assert decision == "ask"
    decision, _ = synth_decision(0.3001, 0.15, 0, CFG)
    assert decision == "allow"


def test_ladder_warn_surcharge_counted_in_cost():
    decision, cost = synth_decision(0.50, 0.15, 2, CFG)
    assert decision == "allow"
    assert cost == pytest.approx(0.55)


def test_ladder_first_match_wins_order():
    # 预算 -0.5 且 warn 命中：deny 段最先命中，cost 仍照算（供审计记录）。
    decision, cost = synth_decision(-0.5, 0.15, 1, CFG)
    assert decision == "deny"
    assert cost == pytest.approx(0.35)
