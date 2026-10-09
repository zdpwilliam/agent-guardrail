import pytest

from guardrail.clock import now_iso
from guardrail.models import EntityDelta
from guardrail.policy.merge import apply_combine, combine_values, field_scalar, merge_entities


def _delta(key: str, **fields) -> EntityDelta:
    return EntityDelta(entity_key=key, last_updated_at=now_iso(), **fields)


# ---------- merge_entities ----------


def test_merge_unions_disjoint_entities():
    a = {"product:p1": _delta("product:p1", price_delta_pct=-25.0)}
    b = {"coupon:CODE": _delta("coupon:CODE", coupon_rate_delta=-20.0)}
    merged = merge_entities(a, b)
    assert set(merged) == {"product:p1", "coupon:CODE"}


def test_merge_sums_same_entity_fields():
    # spec §3.7 的设定：单会话内 Δ 可加——两次降价 5% 就是 -10%。
    a = {"product:p1": _delta("product:p1", price_delta_pct=-5.0)}
    b = {"product:p1": _delta("product:p1", price_delta_pct=-5.0)}
    merged = merge_entities(a, b)
    assert merged["product:p1"].price_delta_pct == -10.0


def test_merge_sums_email_delta_for_same_entity():
    a = {"mailbox:ops@example.com": _delta(
        "mailbox:ops@example.com", email_delta=-1
    )}
    b = {"mailbox:ops@example.com": _delta(
        "mailbox:ops@example.com", email_delta=-2
    )}
    merged = merge_entities(a, b)
    assert merged["mailbox:ops@example.com"].email_delta == -3


def test_merge_keeps_latest_timestamp():
    a = {"product:p1": _delta("product:p1", price_delta_pct=-1.0)}
    a["product:p1"].last_updated_at = "2026-01-01T00:00:00+00:00"
    b = {"product:p1": _delta("product:p1", price_delta_pct=-2.0)}
    b["product:p1"].last_updated_at = "2026-06-01T00:00:00+00:00"
    assert merge_entities(a, b)["product:p1"].last_updated_at == "2026-06-01T00:00:00+00:00"


def test_merge_does_not_mutate_inputs():
    a = {"product:p1": _delta("product:p1", price_delta_pct=-5.0)}
    b = {"product:p1": _delta("product:p1", price_delta_pct=-5.0)}
    merge_entities(a, b)
    assert a["product:p1"].price_delta_pct == -5.0
    assert b["product:p1"].price_delta_pct == -5.0


# ---------- field_scalar ----------


def test_field_scalar_sums_across_entities():
    entities = {
        "product:p1": _delta("product:p1", price_delta_pct=-5.0),
        "product:p2": _delta("product:p2", price_delta_pct=-3.0),
    }
    assert field_scalar(entities, "price_delta_pct") == -8.0


def test_field_scalar_empty_entities_is_zero():
    assert field_scalar({}, "price_delta_pct") == 0.0


def test_field_scalar_stock_is_int_sum():
    entities = {"product:p1": _delta("product:p1", stock_delta=-30)}
    assert field_scalar(entities, "stock_delta") == -30


# ---------- apply_combine ----------


def test_add_operator():
    assert apply_combine("add", -25.0, -20.0) == -45.0


def test_multiplicative_operator_matches_spec_example():
    # spec §3.7 的原例：降价 25% × 8 折券 → 0.75 × 0.80 = 0.60，即 -40%。
    assert apply_combine("multiplicative", -25.0, -20.0) == pytest.approx(-40.0)


def test_multiplicative_with_zero_side_is_identity():
    # 只有定价 Agent 动过、券侧为 0 时，combine 不应凭空放大。
    assert apply_combine("multiplicative", -8.0, 0.0) == pytest.approx(-8.0)


def test_max_operator_is_literal_max():
    # 字面 max(a, b)。对「负值越糟」的字段它取的是较轻的那个——这正是
    # spec §12.1 说的「用错算子 → 漏报」，用错要能被测出来，别替作者兜底。
    assert apply_combine("max", -25.0, -20.0) == -20.0


def test_multiplicative_demo_scenario():
    # 场景 4 的实际数值：-8% 价 × 20% 券 → (0.92)(0.80) - 1 = -26.4%。
    assert apply_combine("multiplicative", -8.0, -20.0) == pytest.approx(-26.4)


# ---------- 用错算子的误报 / 漏报对照（spec §12.1） ----------


def test_wrong_operator_add_overreports():
    # 真值 -40（multiplicative）；加法得 -45 → 过度拦截 → 误伤率上升。
    truth = apply_combine("multiplicative", -25.0, -20.0)
    assert apply_combine("add", -25.0, -20.0) < truth  # -45 < -40，比真值更狠


def test_wrong_operator_max_underreports():
    # 真值 -40；取最大值得 -20 → 漏掉真实亏损 → 拦截率下降。
    truth = apply_combine("multiplicative", -25.0, -20.0)
    assert apply_combine("max", -25.0, -20.0) > truth  # -20 > -40，比真值更轻


# ---------- combine_values ----------


def test_combine_values_takes_most_severe():
    entities = {
        "product:p1": _delta("product:p1", price_delta_pct=-8.0),
        "coupon:CODE": _delta("coupon:CODE", coupon_rate_delta=-20.0),
    }
    entries = [
        {"a": "price_delta_pct", "b": "coupon_rate_delta", "op": "multiplicative"},
        {"a": "price_delta_pct", "b": "price_delta_pct", "op": "add"},
    ]
    # multiplicative → -26.4；add(-8, -8) → -16。取最严重 = 最小值 -26.4。
    assert combine_values(entries, entities) == pytest.approx(-26.4)


def test_combine_values_empty_is_zero():
    assert combine_values([], {}) == 0.0
