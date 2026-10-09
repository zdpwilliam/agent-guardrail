import pytest

from guardrail.models import ActionRecord, EntityDelta, RiskDeltaDecl, SessionState
from guardrail.policy.combined import DELTA_FIELDS
from guardrail.policy.expr import ExpressionError, evaluate
from guardrail.tools.registry import TOOL_SPECS, assert_specs_valid

# ---------- 模型 ----------


def test_entity_delta_defaults():
    d = EntityDelta(entity_key="product:p-1", last_updated_at="2026-10-05T00:00:00+00:00")
    assert d.price_delta_pct == 0.0
    assert d.stock_delta == 0
    assert d.coupon_rate_delta == 0.0


def test_entity_delta_fields_match_canonical_set():
    # combined.py 的 DELTA_FIELDS 与 EntityDelta 的风险字段必须严格一致——
    # 策略引用的字段名以 DELTA_FIELDS 为准，模型漂移了这里先炸。
    risk_fields = set(EntityDelta.model_fields) - {"entity_key", "last_updated_at"}
    assert risk_fields == set(DELTA_FIELDS)


def test_session_state_defaults():
    s = SessionState(session_id="s-1")
    assert s.entities == {}
    assert s.taint == set()
    assert s.actions == []
    assert s.risk_budget == 1.0
    assert s.flagged is False


def test_session_state_roundtrip():
    s = SessionState(
        session_id="s-1",
        entities={"product:p-1": EntityDelta(entity_key="product:p-1", price_delta_pct=-5.0,
                                             last_updated_at="t")},
        taint={"pii"},
        actions=[ActionRecord(seq=1, tool="get_product", args={"product_id": "p-1"},
                              timestamp="t")],
        risk_budget=0.9,
    )
    clone = SessionState.model_validate(s.model_dump())
    assert clone == s


def test_risk_delta_decl_rejects_unknown_field():
    with pytest.raises(ValueError):  # noqa: PT011
        RiskDeltaDecl(
            entity_type="product", entity_arg="product_id",
            field="margin_pct", value_expr="args.x",
        )


# ---------- registry 自检 ----------


def test_update_price_declares_price_delta():
    decls = TOOL_SPECS["update_price"].risk_deltas
    assert len(decls) == 1
    d = decls[0]
    assert (d.entity_type, d.entity_arg, d.field) == ("product", "product_id", "price_delta_pct")
    assert evaluate(d.value_expr, {"delta_pct": -8.0}) == -8.0


def test_update_stock_declares_stock_delta():
    d = TOOL_SPECS["update_stock"].risk_deltas[0]
    assert d.field == "stock_delta"
    assert evaluate(d.value_expr, {"delta": -30}) == -30


def test_create_coupon_declares_negative_coupon_rate():
    # 发 8 折券 = 券贡献 -20：表达式在声明处带符号，提交段不加谜之负号。
    d = TOOL_SPECS["create_coupon"].risk_deltas[0]
    assert d.field == "coupon_rate_delta"
    assert d.entity_arg == "code"
    assert evaluate(d.value_expr, {"discount_pct": 20.0}) == -20.0


def test_create_order_declares_no_risk_delta():
    # 下单不改价、不改库存、不发券（与效果声明同一结论）。
    assert TOOL_SPECS["create_order"].risk_deltas == []


def test_get_order_declares_pii_taint():
    spec = TOOL_SPECS["get_order"]
    assert spec.taint_source is True
    assert spec.taint_categories == ["pii"]


def test_no_other_tool_declares_taint_categories():
    # 污点源按域各领一个：get_order=电商 pii，read_file/read_forum=corp
    # corp_pii（B4 二期：论坛帖是不可信内容，同为污点源）。
    allowed = {"get_order", "read_file", "read_forum"}
    for name, spec in TOOL_SPECS.items():
        if name not in allowed:
            assert spec.taint_categories == [], name


def test_self_check_still_passes():
    assert_specs_valid()


# ---------- 自检的拒绝路径（直接构造违规 spec） ----------


def _spec(**kwargs):
    from guardrail.models import ToolSpec

    base = {
        "name": "t", "kind": "write",
        "args_schema": {"type": "object", "properties": {"x": {"type": "number"}}},
    }
    return ToolSpec(**{**base, **kwargs})


def test_self_check_rejects_delta_arg_not_in_schema():
    from guardrail.tools.registry import _check_risk_deltas

    spec = _spec(risk_deltas=[
        RiskDeltaDecl(entity_type="product", entity_arg="nope",
                      field="price_delta_pct", value_expr="args.nope"),
    ])
    with pytest.raises(RuntimeError, match="nope"):
        _check_risk_deltas("t", spec)


def test_self_check_rejects_delta_expr_unknown_arg():
    from guardrail.tools.registry import _check_risk_deltas

    spec = _spec(risk_deltas=[
        RiskDeltaDecl(entity_type="product", entity_arg="x",
                      field="price_delta_pct", value_expr="args.missing"),
    ])
    with pytest.raises(RuntimeError, match="missing"):
        _check_risk_deltas("t", spec)


def test_self_check_rejects_taint_categories_without_source_flag():
    from guardrail.tools.registry import _check_taint

    spec = _spec(taint_categories=["pii"])
    with pytest.raises(RuntimeError, match="taint_source"):
        _check_taint("t", spec)


# ---------- email_domain 辅助函数 ----------


def test_email_domain_extracts_domain():
    assert evaluate("email_domain(args.to)", {"to": "a@internal.corp"}) == "internal.corp"
    assert evaluate("email_domain(args.to)", {"to": "b@evil.com"}) == "evil.com"


def test_email_domain_is_case_insensitive():
    assert evaluate("email_domain(args.to)", {"to": "a@INTERNAL.CORP"}) == "internal.corp"


def test_email_domain_cannot_escape():
    with pytest.raises(ExpressionError):
        evaluate("email_domain(args.__class__)", {"to": "a@b.c"})
