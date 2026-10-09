from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from guardrail.models import ShadowState
from guardrail.projection import ProjectionError


class PlannedAction(BaseModel):
    tool: str
    args: dict[str, Any] = {}


def _require_float_arg(tool: str, args: dict[str, Any], key: str) -> float:
    """取出并校验一个浮点参数；缺失 / 非数值 / 布尔 / NaN·Inf 都抛 ProjectionError（映射 400）。"""
    if key not in args:
        raise ProjectionError(f"投影失败：动作 {tool!r} 缺少必需参数 {key!r}")
    value = args[key]
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是浮点数，收到 {value!r}"
        )
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 无法转换为浮点数，收到 {value!r}"
        ) from None
    if not math.isfinite(result):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是有限浮点数，收到 {value!r}"
        )
    return result


def _require_int_arg(tool: str, args: dict[str, Any], key: str) -> int:
    """取出并校验一个整数参数；缺失 / 非整数（含非积分浮点）/ 布尔都抛 ProjectionError（映射 400）。

    与商城 Pydantic 的 `int` 字段对齐：积分浮点（-4.0）收敛为 int，非积分浮点
    （1.5）与 `bool` 拒绝——绝不能像裸 `int(1.5)` 那样截断成 1 去投影一份商城会
    拒绝的计划。
    """
    if key not in args:
        raise ProjectionError(f"投影失败：动作 {tool!r} 缺少必需参数 {key!r}")
    value = args[key]
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是整数，收到 {value!r}"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是整数，收到非整数值 {value!r}"
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 无法转换为整数，收到 {value!r}"
        ) from None


def _require_optional_str_arg(tool: str, args: dict[str, Any], key: str) -> str | None:
    """取出并校验一个可选字符串参数；缺失 / `None` 视为「未指名」。

    只有非空 `str` 合法。空串、`0`、`false`、`[]`、`{}` 等既非 `None` 也非
    非空字符串的值都是畸形输入，抛 `ProjectionError`（映射 400），而不是让
    下游的 Pydantic 快照校验把它变成 500。
    """
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if isinstance(value, str) and value != "":
        return value
    raise ProjectionError(
        f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是字符串，收到 {value!r}"
    )


def derive_projection_args(
    action: PlannedAction, shadow: ShadowState, step: int
) -> dict[str, Any]:
    """把模型报的意图参数换算成效果声明需要的派生字段。

    两类补全：

    1. 百分比 → 绝对值（update_price）：效果声明要「价格变化多少分」，
       而模型只说「降价 5%」。基准是**投影后的影子状态**，因为同一商品
       在一个计划里可能被改价多次，第二次的基准必须是第一次之后的价格。
    2. 服务端生成 ID（create_coupon / create_order）：真实执行时 ID 由商城
       生成，预览时用确定性的占位 ID（`preview-coupon-{step}`）。
       占位 ID 只用于聚合指标（毛利、受影响商品数），不影响审批人看到的终态。

    参数本身也必须合法：`delta_pct` / `qty` 缺失或非数值时抛 `ProjectionError`
    （由 `preview_plan` 统一映射为 400），而不是让 `float()` / `int()` 裸抛
    `ValueError` / `KeyError` 变成 500。
    """
    args = dict(action.args)

    if action.tool == "update_price":
        product = shadow.products.get(str(args.get("product_id")))
        if product is not None:
            # 与商城 update_price 的算术逐字对齐：商城存 round(price * (1 + d/100))，
            # 所以绝对变化量必须是 round(price * (1 + d/100)) - price。若改用
            # round(price * d / 100)，当中间值恰好落在 .5 边界时会差一分（
            # before=9、d=50 时商城存 14、旧公式得 13）。
            delta_pct = _require_float_arg(action.tool, args, "delta_pct")
            # 输入有限不代表中间量有限：1e305 能让 price * (1 + d/100) 溢出为 inf，
            # round(inf) 抛 OverflowError（不是 ProjectionError）会逃逸成 500。
            # 守卫必须落在进入 round() 的那个值上，而不只是解析后的输入。
            target_cents = product.list_price_cents * (1 + delta_pct / 100)
            if not math.isfinite(target_cents):
                raise ProjectionError(
                    f"投影失败：动作 {action.tool!r} 的参数 'delta_pct' "
                    f"使价格计算溢出为非有限值，收到 {delta_pct!r}"
                )
            args["absolute_delta_cents"] = round(target_cents) - product.list_price_cents

    elif action.tool == "create_coupon":
        args["coupon_id"] = f"preview-coupon-{step}"

    elif action.tool == "create_order":
        args["order_id"] = f"preview-order-{step}"
        qty = _require_int_arg(action.tool, args, "qty")
        args["qty"] = qty
        product = shadow.products.get(str(args.get("product_id")))
        unit_price = product.list_price_cents if product is not None else 0
        coupon_id = _require_optional_str_arg(action.tool, args, "coupon_id")
        # 显式写入 args（None 表示未带券），供 create_order 的券用量递增效果
        # （optional=True）判定是否该动券——与商城「带券才改 used」对齐。
        args["coupon_id"] = coupon_id
        if coupon_id is not None:
            coupon = shadow.coupons.get(coupon_id)
            if coupon is not None:
                unit_price = round(unit_price * (1 - coupon.discount_pct / 100))
        args["unit_price_cents"] = unit_price

    return args
