import ast

import pytest

from guardrail.policy.expr import (
    MAX_NODES,
    ExpressionError,
    arg_names,
    evaluate,
    parse,
)

# ---------- 正常求值 ----------


@pytest.mark.parametrize(
    ("expression", "args", "expected"),
    [
        ("abs(args.delta_pct) > 10", {"delta_pct": -50}, True),
        ("abs(args.delta_pct) > 10", {"delta_pct": -5}, False),
        ("args.discount_pct > 80", {"discount_pct": 90.0}, True),
        ("args.qty >= 1 and args.qty <= 10", {"qty": 3}, True),
        ("args.qty >= 1 and args.qty <= 10", {"qty": 30}, False),
        ("min(args.a, args.b) <= 0", {"a": 5, "b": 0}, True),
        ("max(args.a, args.b) > 100", {"a": 5, "b": 200}, True),
        ("len(args.code) > 0", {"code": "S20"}, True),
        ("round(args.pct) == 20", {"pct": 19.6}, True),
        ("args.qty in [1, 2, 3]", {"qty": 2}, True),
        ("args.qty in [1, 2, 3]", {"qty": 9}, False),
        ("args.flag", {"flag": False}, False),
        ("not args.flag", {"flag": False}, True),
        ("args.a if args.b else args.c", {"a": 1, "b": True, "c": 2}, 1),
    ],
)
def test_evaluate_supported_expressions(expression, args, expected):
    assert evaluate(expression, args) == expected


def test_absent_optional_arg_reads_as_none():
    # args.coupon_id 在 schema 里是可选参数，模型不下发时必须读作 None，
    # 否则 `args.coupon_id != null` 会在无券订单上直接求值失败。
    assert evaluate("args.coupon_id != null", {}) is False
    assert evaluate("args.coupon_id != null", {"coupon_id": "c-1"}) is True


@pytest.mark.parametrize(
    ("expression", "args"),
    [
        ("abs(args.missing)", {}),
        ("args.a + args.b", {"a": "x", "b": 1}),
        ("args.a > 1", {"a": None}),
    ],
)
def test_evaluate_runtime_error_becomes_expression_error(expression, args):
    # 求值期出错必须变成 ExpressionError，调用方据此 fail-closed 拒绝，
    # 绝不能让 TypeError 逃逸成 500。
    with pytest.raises(ExpressionError):
        evaluate(expression, args)


# ---------- YAML 风格的字面量 ----------


def test_yaml_style_literals_are_bound():
    assert evaluate("args.x == null", {"x": None}) is True
    assert evaluate("args.x == true", {"x": True}) is True
    assert evaluate("args.x == false", {"x": False}) is True


# ---------- 沙箱逃逸 ----------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "args.__class__",
        "args.__class__.__mro__",
        "open('/etc/passwd')",
        "eval('1+1')",
        "args.__dict__",
        "[x for x in args]",
        "(lambda: 1)()",
        "args.a if args.b else __import__('os')",
        "f'{args.a}'",
        "args.a.b",
        "args['a']",
        "args.a; import os",
    ],
)
def test_evaluate_rejects_escapes(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression, {"a": 1, "b": True})


def test_evaluate_rejects_unknown_function():
    with pytest.raises(ExpressionError):
        evaluate("dir(args)", {})


def test_evaluate_rejects_keyword_arguments():
    with pytest.raises(ExpressionError):
        evaluate("round(args.x, ndigits=2)", {"x": 1.234})


def test_evaluate_rejects_unknown_name():
    with pytest.raises(ExpressionError):
        evaluate("args.x + secret", {"x": 1})


def test_evaluate_rejects_syntax_error():
    with pytest.raises(ExpressionError):
        evaluate("args.x >", {"x": 1})


def test_builtins_are_not_reachable_even_if_name_check_were_skipped():
    # 名字白名单是第一道防线，清空 builtins 是第二道：两道都要在。
    with pytest.raises(ExpressionError):
        evaluate("__builtins__", {})


# ---------- 复杂度上限 ----------


def test_expression_node_count_is_capped():
    bomb = "1" + "".join(f" + {i}" for i in range(MAX_NODES + 10))
    with pytest.raises(ExpressionError, match="过于复杂"):
        evaluate(bomb, {})


def test_parse_returns_expression_node():
    assert isinstance(parse("args.x > 1"), ast.Expression)


def test_arg_names_extracts_referenced_fields():
    assert arg_names("abs(args.delta_pct) > 10 and args.qty < 3") == {"delta_pct", "qty"}
    assert arg_names("args.x == null") == {"x"}
    assert arg_names("1 > 0") == set()


def test_arg_names_rejects_invalid_expression():
    with pytest.raises(ExpressionError):
        arg_names("args.__class__ > 1")
