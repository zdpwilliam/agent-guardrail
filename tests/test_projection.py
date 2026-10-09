import pytest

from guardrail.models import EffectOp, ToolSpec
from guardrail.projection import (
    ProjectionError,
    apply_effect,
    build_shadow,
    project,
    resolve_template,
)


def state_with_price(price_cents=10000, stock=10):
    return build_shadow(
        products=[
            {
                "id": "p1",
                "name": "T",
                "category": "c",
                "cost_price_cents": 6000,
                "list_price_cents": price_cents,
                "stock": stock,
            }
        ],
        coupons=[],
        orders=[],
    )


def test_resolve_template_pulls_from_args():
    assert resolve_template("{args.delta_pct}", {"delta_pct": -10}) == -10
    assert resolve_template("literal", {"delta_pct": -10}) == "literal"


def test_apply_effect_add_mutates_in_place():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.absolute_delta_cents}",
    )
    apply_effect(s, op, {"product_id": "p1", "absolute_delta_cents": -1000})
    assert s.products["p1"].list_price_cents == 9000
    assert "product:p1" in s.touched


def test_project_single_price_change_does_not_mutate_input():
    s = state_with_price()
    tools = {
        "update_price": ToolSpec(
            name="update_price",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="list_price_cents",
                    op="add",
                    value="{args.absolute_delta_cents}",
                )
            ],
        )
    }
    out = project(
        s, tools, [("update_price", {"product_id": "p1", "absolute_delta_cents": -1000})]
    )
    assert out.products["p1"].list_price_cents == 9000
    assert s.products["p1"].list_price_cents == 10000


def test_project_unknown_tool_raises():
    with pytest.raises(KeyError):
        project(state_with_price(), {}, [("nope", {})])


def test_apply_effect_set_status_on_order():
    s = build_shadow(
        products=[],
        coupons=[],
        orders=[
            {
                "id": "o1",
                "product_id": "p1",
                "qty": 1,
                "unit_price_cents": 100,
                "coupon_id": None,
                "status": "created",
            }
        ],
    )
    op = EffectOp(target="order:{args.order_id}", field="status", op="set", value="refunded")
    apply_effect(s, op, {"order_id": "o1"})
    assert s.orders["o1"].status == "refunded"


def test_apply_effect_append_coupon():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    apply_effect(s, op, {"coupon_id": "c1", "code": "X", "discount_pct": 20.0, "max_uses": 5})
    assert s.coupons["c1"].discount_pct == 20.0
    assert "coupon:c1" in s.touched


def test_apply_effect_missing_entity_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.absolute_delta_cents}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p-missing", "absolute_delta_cents": -1000})


def test_apply_effect_unknown_field_raises():
    s = state_with_price()
    op = EffectOp(target="product:{args.product_id}", field="nonexistent", op="set", value="x")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})


def test_apply_effect_unresolved_template_variable_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.missing}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})


def test_apply_effect_unknown_entity_type_raises():
    s = state_with_price()
    op = EffectOp(target="widget:{args.widget_id}", field="stock", op="set", value=5)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"widget_id": "w1"})


def test_touched_not_recorded_when_effect_raises():
    s = state_with_price()
    op = EffectOp(target="product:{args.product_id}", field="nonexistent", op="set", value="x")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})
    assert "product:p1" not in s.touched


def test_project_mid_sequence_raise_leaves_input_untouched():
    s = state_with_price()
    tools = {
        "update_price": ToolSpec(
            name="update_price",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="list_price_cents",
                    op="add",
                    value="{args.absolute_delta_cents}",
                )
            ],
        ),
        "break_it": ToolSpec(
            name="break_it",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="nonexistent",
                    op="set",
                    value="x",
                )
            ],
        ),
    }
    calls = [
        ("update_price", {"product_id": "p1", "absolute_delta_cents": -1000}),
        ("break_it", {"product_id": "p1"}),
    ]
    before = s.model_copy(deep=True)
    with pytest.raises(ProjectionError):
        project(s, tools, calls)
    assert s == before


def test_apply_effect_empty_entity_id_raises():
    s = state_with_price()
    op = EffectOp(target="product:", field="stock", op="set", value=1)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {})


def test_apply_effect_add_non_numeric_value_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": "not-a-number"})


def test_apply_effect_mul_non_numeric_field_raises():
    s = build_shadow(
        products=[],
        coupons=[],
        orders=[
            {
                "id": "o1",
                "product_id": "p1",
                "qty": 1,
                "unit_price_cents": 100,
                "coupon_id": None,
                "status": "created",
            }
        ],
    )
    op = EffectOp(target="order:{args.order_id}", field="status", op="mul", value=0.5)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"order_id": "o1"})


def test_apply_effect_non_numeric_add_leaves_field_unchanged():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": "not-a-number"})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_non_numeric_mul_leaves_field_unchanged():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="mul",
        value="{args.factor}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "factor": "two"})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_append_coupon_missing_key_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"coupon_id": "c1", "code": "X", "max_uses": 5})


def test_apply_effect_append_coupon_bad_discount_pct_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": "abc", "max_uses": 5}
        )


def test_apply_effect_append_order_missing_key_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"order_id": "o1", "product_id": "p1", "unit_price_cents": 100})


def test_apply_effect_append_order_fractional_cents_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s,
            op,
            {"order_id": "o1", "product_id": "p1", "qty": 1, "unit_price_cents": 100.5},
        )


def test_apply_effect_add_fractional_cents_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": -1000.5})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_add_integral_float_cents_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"product_id": "p1", "delta": -1000.0})
    assert s.products["p1"].list_price_cents == 9000
    assert isinstance(s.products["p1"].list_price_cents, int)


def test_apply_effect_set_fractional_cents_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="set",
        value="{args.price}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "price": 12345.5})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_set_integral_float_cents_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="set",
        value="{args.price}",
    )
    apply_effect(s, op, {"product_id": "p1", "price": 12345.0})
    assert s.products["p1"].list_price_cents == 12345
    assert isinstance(s.products["p1"].list_price_cents, int)


def test_apply_effect_add_non_integral_int_field_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="stock",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": -3.5})
    assert s.products["p1"].stock == 10


def test_apply_effect_add_integral_float_int_field_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="stock",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"product_id": "p1", "delta": -4.0})
    assert s.products["p1"].stock == 6
    assert isinstance(s.products["p1"].stock, int)


def test_apply_effect_add_float_field_keeps_float():
    s = build_shadow(
        products=[],
        coupons=[
            {
                "id": "c1",
                "code": "X",
                "discount_pct": 10.0,
                "max_uses": 5,
                "used": 0,
            }
        ],
        orders=[],
    )
    op = EffectOp(
        target="coupon:{args.coupon_id}",
        field="discount_pct",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"coupon_id": "c1", "delta": 5.5})
    assert s.coupons["c1"].discount_pct == 15.5


def test_apply_effect_append_coupon_bool_max_uses_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": 20.0, "max_uses": True}
        )


def test_apply_effect_append_coupon_bool_discount_pct_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": True, "max_uses": 5}
        )


def test_apply_effect_append_order_bool_qty_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s,
            op,
            {"order_id": "o1", "product_id": "p1", "qty": True, "unit_price_cents": 100},
        )
