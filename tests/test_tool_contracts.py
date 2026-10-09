import pytest

from guardrail.models import EntityEmit, EntityRequire
from guardrail.tools.contracts import ArgsValidationError, validate_args
from guardrail.tools.registry import TOOL_SPECS


def _spec(tool: str):
    return TOOL_SPECS[tool]


# ---------- 正常 ----------


def test_valid_update_price_args_pass():
    validate_args(_spec("update_price"), {"product_id": "p-1", "delta_pct": -5.0})


def test_optional_coupon_id_may_be_absent_or_null():
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1})
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1, "coupon_id": None})
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1, "coupon_id": "c-1"})


def test_list_products_accepts_empty_args():
    validate_args(_spec("list_products"), {})


# ---------- 违规 ----------


def test_missing_required_arg_raises():
    with pytest.raises(ArgsValidationError, match="product_id"):
        validate_args(_spec("get_product"), {})


def test_unknown_arg_raises():
    # 未知参数一律拒绝：模型多报一个字段通常意味着它在编造，而服务端派生的
    # 字段（absolute_delta_cents）绝不能由模型自己塞进来。
    with pytest.raises(ArgsValidationError):
        validate_args(_spec("update_price"), {"product_id": "p", "delta_pct": -1, "extra": 1})


def test_model_cannot_inject_server_derived_field():
    with pytest.raises(ArgsValidationError):
        validate_args(
            _spec("update_price"),
            {"product_id": "p", "delta_pct": -1, "absolute_delta_cents": -999999},
        )


def test_string_delta_pct_raises():
    with pytest.raises(ArgsValidationError):
        validate_args(_spec("update_price"), {"product_id": "p", "delta_pct": "-5"})


def test_non_integer_qty_raises():
    with pytest.raises(ArgsValidationError, match="qty"):
        validate_args(_spec("create_order"), {"product_id": "p", "qty": 1.5})


def test_zero_qty_raises():
    with pytest.raises(ArgsValidationError, match="qty"):
        validate_args(_spec("create_order"), {"product_id": "p", "qty": 0})


def test_empty_product_id_raises():
    with pytest.raises(ArgsValidationError, match="product_id"):
        validate_args(_spec("get_product"), {"product_id": ""})


def test_discount_pct_out_of_range_raises():
    with pytest.raises(ArgsValidationError, match="discount_pct"):
        validate_args(
            _spec("create_coupon"), {"code": "X", "discount_pct": 120.0, "max_uses": 1}
        )


def test_error_message_names_the_offending_field():
    with pytest.raises(ArgsValidationError) as exc:
        validate_args(_spec("create_coupon"), {"code": "X", "discount_pct": 20.0, "max_uses": 0})
    assert "max_uses" in str(exc.value)


# ---------- 契约本身的完整性 ----------


def test_every_tool_declares_args_schema():
    for name, spec in TOOL_SPECS.items():
        assert spec.args_schema.get("type") == "object", name
        assert spec.args_schema.get("additionalProperties") is False, name


def test_every_write_tool_declares_its_entity_requirements():
    assert {r.arg for r in TOOL_SPECS["update_price"].requires} == {"product_id"}
    assert {r.arg for r in TOOL_SPECS["refund_order"].requires} == {"order_id"}
    order_reqs = {r.arg: r for r in TOOL_SPECS["create_order"].requires}
    assert set(order_reqs) == {"product_id", "coupon_id"}
    # 券是可选依赖：没带券就不该要求 coupon_id 已被本会话读过。
    assert order_reqs["coupon_id"].optional is True
    assert order_reqs["product_id"].optional is False


def test_read_tools_have_no_requirements():
    for name in ("list_products", "get_product", "get_order"):
        assert TOOL_SPECS[name].requires == []


def test_emit_paths_point_at_plausible_top_level_keys():
    emits = {e.entity_type: e.path for e in TOOL_SPECS["get_product"].emits}
    assert emits == {"product": "product.id"}
    order = {e.entity_type: e.path for e in TOOL_SPECS["create_order"].emits}
    assert order["order"] == "order_id"
    assert order["product"] == "order.product_id"
    listing = {e.entity_type: e.path for e in TOOL_SPECS["list_products"].emits}
    assert listing == {"product": "products[].id"}


def test_send_email_has_neither_emits_nor_requires():
    assert TOOL_SPECS["send_email"].emits == []
    assert TOOL_SPECS["send_email"].requires == []


def test_entity_models_reject_unknown_type():
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic.ValidationError 的基类断言
        EntityEmit(entity_type="invoice", path="x")


def test_require_defaults_to_mandatory():
    assert EntityRequire(entity_type="product", arg="product_id").optional is False
