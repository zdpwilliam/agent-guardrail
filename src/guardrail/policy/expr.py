from __future__ import annotations

import ast
from typing import Any

# 节点数上限。一条策略表达式正常在 10 个节点以内；300 个节点足够表达任何
# 业务阈值，同时让「构造一颗解析炸弹」这件事在加载期就被挡住，而不是在
# 每次调用时烧 CPU。
MAX_NODES = 300

# 函数白名单。刻意保持极小：策略表达式只需要这几个，多一个就多一个能被
# 用来做类型混淆或算术放大的入口。
#
# email_domain 是第一个「策略辅助函数」：污点规则的 sink_condition 需要提取
# 邮箱域名（spec §3.4③），而通用属性访问/下标在沙箱里是被禁的。辅助函数必须
# 是显式注册的纯函数——每加一个都要过一次安全审视，这是扩展沙箱的唯一途径。
def _email_domain(value: object) -> str:
    return str(value).rpartition("@")[2].lower()


_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "email_domain": _email_domain,
}

# YAML/JSON 风格的字面量。策略文件读起来更像配置而不是 Python——
# spec §3.4 的样例写的就是 `args.coupon_id != null`。
_CONSTANTS: dict[str, Any] = {"null": None, "true": True, "false": False}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Attribute,
    ast.IfExp,
    ast.Tuple,
    ast.List,
)


class ExpressionError(Exception):
    """表达式非法或求值失败。调用方必须按 fail-closed 处理（spec §10.2）。"""


def _check_attribute(node: ast.Attribute) -> None:
    """只允许 `args.<字段>`。

    属性访问是沙箱逃逸的主要入口（`().__class__.__bases__` 一路走到 object
    就能拿到一切）。所以这里不是「禁掉属性访问」，而是把接收者钉死为 `args`，
    并禁掉下划线开头的名字。
    """
    if not isinstance(node.value, ast.Name) or node.value.id != "args":
        raise ExpressionError("只允许访问 args 的字段")
    if node.attr.startswith("_"):
        raise ExpressionError(f"禁止访问下划线开头的字段：{node.attr}")


def _check_name(node: ast.Name) -> None:
    if node.id == "args" or node.id in _ALLOWED_FUNCS or node.id in _CONSTANTS:
        return
    raise ExpressionError(f"表达式引用了未知名字：{node.id}")


def _check_call(node: ast.Call) -> None:
    # 只允许 `f(...)` 的直接形式：`args.foo()` 这类带接收者的调用已被
    # _check_attribute 拦下，*args / **kwargs 也一并拒绝。
    if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
        raise ExpressionError("只允许调用白名单内的函数")
    if node.keywords:
        raise ExpressionError("表达式不支持关键字参数")


def _validate(tree: ast.Expression) -> None:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise ExpressionError(f"表达式过于复杂：{len(nodes)} 个节点，上限 {MAX_NODES}")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"表达式含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Attribute):
            _check_attribute(node)
        elif isinstance(node, ast.Name):
            _check_name(node)
        elif isinstance(node, ast.Call):
            _check_call(node)


def parse(expression: str) -> ast.Expression:
    """解析并静态校验一条策略表达式。"""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误：{exc.msg}") from exc
    _validate(tree)
    return tree


def arg_names(expression: str) -> set[str]:
    """表达式引用了哪些 `args` 字段。

    策略 lint 用它对照工具的参数契约抓拼写错误（`args.delt_pct` 这类）——
    没有这一步，拼错的字段会静默读作 None，规则从此永不触发。
    """
    return {n.attr for n in ast.walk(parse(expression)) if isinstance(n, ast.Attribute)}


class _Args:
    """表达式看到的 args 视图。

    缺失的可选参数读作 `None`——这样 `args.coupon_id != null` 才能在「没带券」
    的订单上正常求值。真正的类型错误（比如 `abs(None)`）会在 evaluate 里变成
    ExpressionError，由调用方 fail-closed。拼写错误由 lint 拦，不靠这里。
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - 表达式求值天然是动态类型
        # 只在常规属性查找失败时触发，因此不会与 _values 打架。
        if name.startswith("_"):
            raise ExpressionError(f"禁止访问下划线开头的字段：{name}")
        return self._values.get(name)


def evaluate(expression: str, args: dict[str, Any]) -> Any:  # noqa: ANN401
    """求值一条策略表达式。静态校验 + 动态求值，任一环节出错都抛 ExpressionError。"""
    tree = parse(expression)
    namespace: dict[str, Any] = {
        **_ALLOWED_FUNCS,
        **_CONSTANTS,
        "args": _Args(args),
        # 名字白名单已经是第一道防线，这里清空 builtins 是第二道。eval 会
        # 往 globals 里塞 __builtins__，不清空就等于把 open / __import__
        # 交到一个「万一白名单被绕过」的表达式手里。
        "__builtins__": {},
    }
    try:
        return eval(compile(tree, filename="<policy>", mode="eval"), namespace)  # noqa: S307
    except ExpressionError:
        raise
    except Exception as exc:
        raise ExpressionError(f"表达式求值失败：{type(exc).__name__}: {exc}") from exc
