import pytest

from guardrail.models import EffectOp, ToolSpec
from guardrail.tools.registry import TOOL_SPECS, assert_specs_valid


def _specs_with_effect(effect: EffectOp) -> dict[str, ToolSpec]:
    """在真实 update_price spec 的基础上换掉效果声明。

    必须继承真实 spec 的 args_schema / emits / requires，否则自检会先在参数
    契约上报错，测试就测不到效果声明那一层了。
    """
    specs = dict(TOOL_SPECS)
    base = TOOL_SPECS["update_price"]
    specs["update_price"] = base.model_copy(update={"effects": [effect]})
    return specs


def test_exactly_sixteen_tools():
    """9 个电商工具 + 7 个 corp 域工具（B4 二期扩 4 个；send_email 两域复用）。"""
    assert len(TOOL_SPECS) == 16


def test_expected_tool_names():
    assert set(TOOL_SPECS) == {
        "list_products",
        "get_product",
        "get_order",
        "update_price",
        "update_stock",
        "create_coupon",
        "create_order",
        "refund_order",
        "send_email",
        # corp 域（v0.3 起，B4 二期扩 4 个）
        "list_files",
        "read_file",
        "write_file",
        "read_forum",
        "post_forum",
        "create_ticket",
        "execute_wire",
    }


def test_read_tools_have_no_effects():
    for name in ("list_products", "get_product", "get_order"):
        assert TOOL_SPECS[name].effects == []


def test_write_tools_declare_expected_fields():
    assert {op.field for op in TOOL_SPECS["update_price"].effects} == {"list_price_cents"}
    assert {op.field for op in TOOL_SPECS["update_stock"].effects} == {"stock"}


def test_taint_source_and_sink_are_declared():
    assert TOOL_SPECS["get_order"].taint_source is True
    assert TOOL_SPECS["send_email"].taint_sink is True


def test_assert_specs_valid_passes_on_shipped_registry():
    assert_specs_valid()


@pytest.mark.parametrize("target", ["productx:{args.product_id}", ":x"])
def test_assert_specs_valid_rejects_unknown_entity_type(target: str):
    specs = _specs_with_effect(EffectOp(target=target, field="list_price_cents", op="add"))
    with pytest.raises(RuntimeError, match="实体类型未知"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_empty_entity_id():
    specs = _specs_with_effect(EffectOp(target="product:", field="list_price_cents", op="add"))
    with pytest.raises(RuntimeError, match="实体 id 为空"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_unknown_field():
    specs = _specs_with_effect(
        EffectOp(target="product:{args.product_id}", field="list_price_centss", op="add")
    )
    with pytest.raises(RuntimeError, match="字段"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_append_on_product():
    specs = _specs_with_effect(
        EffectOp(target="product:{args.product_id}", field="", op="append")
    )
    with pytest.raises(RuntimeError, match="append"):
        assert_specs_valid(specs)
