from __future__ import annotations

import math
import re
from typing import Any

from guardrail.models import (
    CouponSnapshot,
    EffectOp,
    OrderSnapshot,
    ProductSnapshot,
    ShadowState,
    ToolSpec,
)

_TEMPLATE_RE = re.compile(r"^\{args\.([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_INLINE_RE = re.compile(r"\{args\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class ProjectionError(Exception):
    """投影无法应用某个效果时抛出；网关 API 会将其映射为 HTTP 400。"""


def resolve_template(value: object, args: dict[str, Any]) -> object:
    """'{args.x}' → args['x']；其他原样返回。"""
    if isinstance(value, str):
        m = _TEMPLATE_RE.match(value)
        if m:
            return args[m.group(1)]
    return value


def _template_vars(text: str) -> list[str]:
    """提取字符串里所有 '{args.xxx}' 模板变量名。"""
    return _INLINE_RE.findall(text)


def _resolve_target(target: str, args: dict[str, Any]) -> tuple[str, str]:
    """'product:{args.product_id}' → ('product', 'p1')。"""
    resolved = _INLINE_RE.sub(lambda m: str(args.get(m.group(1), "")), target)
    entity_type, _, entity_id = resolved.partition(":")
    return entity_type, entity_id


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_numeric(value: object, target: str, op: str) -> None:
    if not _is_number(value):
        raise ProjectionError(f"投影失败：效果 {target!r} 的 {op} 操作需要数值，但收到 {value!r}")


def _require_key(args: dict[str, Any], key: str, target: str) -> object:
    """取出 append 必需的参数；缺失时抛出 ProjectionError，而不是静默兜底。"""
    if key not in args:
        raise ProjectionError(f"投影失败：效果 {target!r} 的 append 缺少必需参数 {key!r}")
    return args[key]


def _coerce_str(value: object, key: str, target: str) -> str:
    if not isinstance(value, str):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 必须是字符串，收到 {value!r}"
        )
    return value


def _coerce_float(value: object, key: str, target: str) -> float:
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 必须是浮点数，收到 {value!r}"
        )
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 无法转换为浮点数，收到 {value!r}"
        ) from None
    if not math.isfinite(result):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 必须是有限浮点数，收到 {value!r}"
        )
    return result


def _coerce_int(value: object, key: str, target: str) -> int:
    return _coerce_integral(value, key, target)


def _coerce_integral(value: object, key: str, target: str, *, noun: str = "整数") -> int:
    """把整数字段的值收敛为 int：积分浮点（-4.0）转 int，非积分与 bool 抛错。"""
    if isinstance(value, bool):
        raise ProjectionError(f"投影失败：效果 {target!r} 的 {key!r} 必须是{noun}，收到 {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 {key!r} 必须是{noun}，收到非整数值 {value!r}"
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 {key!r} 无法转换为{noun}，收到 {value!r}"
        ) from None


def _coerce_cents(value: object, key: str, target: str) -> int:
    """把 *_cents 字段的值收敛为整数分：积分浮点（-1000.0）转 int，非积分抛错。"""
    return _coerce_integral(value, key, target, noun="整数金额分")


def apply_effect(state: ShadowState, op: EffectOp, args: dict[str, Any]) -> None:
    """把一条效果声明应用到影子状态上（原地修改）。无法应用时抛出 ProjectionError。"""
    for var in _template_vars(op.target):
        if var not in args:
            raise ProjectionError(
                f"投影失败：效果 {op.target!r} 引用的模板变量 {var!r} 未在 args 中提供"
            )
    if isinstance(op.value, str):
        for var in _template_vars(op.value):
            if var not in args:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 引用的模板变量 {var!r} 未在 args 中提供"
                )

    if op.optional and any(args.get(var) is None for var in _template_vars(op.target)):
        # 可选引用未指名（如 create_order 未带 coupon_id）：真实商城此时不动任何券，
        # 投影同样必须是无操作，而不是报错或误改别的实体。
        return

    entity_type, entity_id = _resolve_target(op.target, args)
    if not entity_id:
        raise ProjectionError(f"投影失败：效果 {op.target!r} 解析出的实体 id 为空")
    if entity_type not in ("product", "coupon", "order"):
        raise ProjectionError(f"投影失败：效果 {op.target!r} 的实体类型 {entity_type!r} 未知")
    value = resolve_template(op.value, args)

    if entity_type == "product":
        if op.op == "append":
            raise ProjectionError(f"投影失败：append 仅支持 coupon / order，收到 {op.target!r}")
        product = state.products.get(entity_id)
        if product is None:
            raise ProjectionError(
                f"投影失败：效果 {op.target!r} 的目标 product {entity_id!r} 不在影子状态中"
            )
        _apply_field(product, op.field, op.op, value, op.target)
    elif entity_type == "coupon":
        if op.op == "append":
            state.coupons[entity_id] = CouponSnapshot(
                coupon_id=entity_id,
                code=_coerce_str(_require_key(args, "code", op.target), "code", op.target),
                discount_pct=_coerce_float(
                    _require_key(args, "discount_pct", op.target), "discount_pct", op.target
                ),
                max_uses=_coerce_int(
                    _require_key(args, "max_uses", op.target), "max_uses", op.target
                ),
                used=0,
            )
        else:
            coupon = state.coupons.get(entity_id)
            if coupon is None:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 的目标 coupon {entity_id!r} 不在影子状态中"
                )
            _apply_field(coupon, op.field, op.op, value, op.target)
    else:  # order
        if op.op == "append":
            state.orders[entity_id] = OrderSnapshot(
                order_id=entity_id,
                product_id=_coerce_str(
                    _require_key(args, "product_id", op.target), "product_id", op.target
                ),
                qty=_coerce_int(_require_key(args, "qty", op.target), "qty", op.target),
                unit_price_cents=_coerce_cents(
                    _require_key(args, "unit_price_cents", op.target),
                    "unit_price_cents",
                    op.target,
                ),
                coupon_id=args.get("coupon_id"),
                status="created",
            )
        else:
            order = state.orders.get(entity_id)
            if order is None:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 的目标 order {entity_id!r} 不在影子状态中"
                )
            _apply_field(order, op.field, op.op, value, op.target)

    state.touched.add(f"{entity_type}:{entity_id}")


def _apply_field(obj: object, field: str, op: str, value: object, target: str) -> None:
    if not field or not hasattr(obj, field):
        raise ProjectionError(f"投影失败：效果 {target!r} 的字段 {field!r} 在目标对象上不存在")
    current = getattr(obj, field)
    if op == "add":
        _require_numeric(value, target, op)
        _require_numeric(current, target, op)
        result = current + value
        if field.endswith("_cents"):
            result = _coerce_cents(result, field, target)
        elif isinstance(current, int) and not isinstance(current, bool):
            result = _coerce_integral(result, field, target)
        setattr(obj, field, result)
    elif op == "set":
        if field.endswith("_cents"):
            value = _coerce_cents(value, field, target)
        setattr(obj, field, value)
    elif op == "mul":
        _require_numeric(value, target, op)
        _require_numeric(current, target, op)
        result = round(current * value)
        if field.endswith("_cents"):
            result = _coerce_cents(result, field, target)
        elif isinstance(current, int) and not isinstance(current, bool):
            result = _coerce_integral(result, field, target)
        setattr(obj, field, result)
    else:
        raise ProjectionError(f"投影失败：效果 {target!r} 使用了不支持的 op {op!r}")


def project(
    state: ShadowState, tools: dict[str, ToolSpec], calls: list[tuple[str, dict[str, Any]]]
) -> ShadowState:
    """在影子状态上按序执行动作序列。绝不修改传入的 state。"""
    shadow = state.clone()
    for tool_name, args in calls:
        spec = tools[tool_name]
        for op in spec.effects:
            apply_effect(shadow, op, args)
    return shadow


def build_shadow(
    products: list[dict[str, Any]], coupons: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> ShadowState:
    """从商城的行数据构建影子状态。"""
    return ShadowState(
        products={
            p["id"]: ProductSnapshot(
                product_id=p["id"],
                cost_price_cents=p["cost_price_cents"],
                list_price_cents=p["list_price_cents"],
                stock=p["stock"],
            )
            for p in products
        },
        coupons={
            c["id"]: CouponSnapshot(
                coupon_id=c["id"],
                code=c["code"],
                discount_pct=c["discount_pct"],
                max_uses=c["max_uses"],
                used=c["used"],
            )
            for c in coupons
        },
        orders={
            o["id"]: OrderSnapshot(
                order_id=o["id"],
                product_id=o["product_id"],
                qty=o["qty"],
                unit_price_cents=o["unit_price_cents"],
                coupon_id=o["coupon_id"],
                status=o["status"],
            )
            for o in orders
        },
    )
