from __future__ import annotations

import math
from typing import Any

from guardrail.models import ShadowState
from guardrail.projection import ProjectionError


def assert_action_applicable(tool: str, args: dict[str, Any], shadow: ShadowState) -> None:
    """校验一个动作在当前影子状态下是否满足商城领域不变量。

    与 shop/store.py 的写操作逐条对齐（有意重复）：商城会拒绝的动作，预览也
    必须拒绝，否则审批人会被一份真实执行会 409/404 的终态欺骗。类型合法性
    （非数值、非整数）不在此处职责内，由投影引擎的 _coerce_* 负责拒绝。
    """
    if tool == "update_price":
        _check_update_price(args, shadow)
    elif tool == "update_stock":
        _check_update_stock(args, shadow)
    elif tool == "create_coupon":
        _check_create_coupon(args)
    elif tool == "create_order":
        _check_create_order(args, shadow)
    elif tool == "refund_order":
        _check_refund_order(args, shadow)


def _check_update_price(args: dict[str, Any], shadow: ShadowState) -> None:
    product = shadow.products.get(str(args.get("product_id", "")))
    if product is None:
        return
    # 与 derive_projection_args 同样的守卫：delta_pct 有限（如 1e305）也可能让
    # 中间量溢出为 inf，round(inf) 抛 OverflowError → 逃逸成 500。守卫中间量本身。
    target_cents = product.list_price_cents * (1 + float(args["delta_pct"]) / 100)
    if not math.isfinite(target_cents):
        raise ProjectionError(
            "预览拒绝：动作 'update_price' 的参数 'delta_pct' "
            f"使价格计算溢出为非有限值，收到 {args['delta_pct']!r}"
        )
    new_price = round(target_cents)
    if new_price <= 0:
        raise ProjectionError(f"预览拒绝：改价后价格非正: {new_price}")


def _check_update_stock(args: dict[str, Any], shadow: ShadowState) -> None:
    product = shadow.products.get(str(args.get("product_id", "")))
    if product is None:
        return
    delta = args.get("delta")
    if not isinstance(delta, int) or isinstance(delta, bool):
        return
    new_stock = product.stock + delta
    if new_stock < 0:
        raise ProjectionError(f"预览拒绝：库存不能为负: {new_stock}")


def _check_create_coupon(args: dict[str, Any]) -> None:
    discount_pct = args.get("discount_pct")
    if not isinstance(discount_pct, (int, float)) or isinstance(discount_pct, bool):
        return
    if not 0 < discount_pct < 100:
        raise ProjectionError(f"预览拒绝：折扣率越界: {discount_pct}")


def _check_create_order(args: dict[str, Any], shadow: ShadowState) -> None:
    qty = args.get("qty")
    if isinstance(qty, int) and not isinstance(qty, bool) and qty <= 0:
        raise ProjectionError(f"预览拒绝：订单数量必须为正，收到 {qty!r}")

    product_id = str(args.get("product_id", ""))
    if product_id not in shadow.products:
        raise ProjectionError(f"预览拒绝：商品不存在: {product_id}")

    coupon_id = args.get("coupon_id")
    if coupon_id is not None:
        coupon = shadow.coupons.get(str(coupon_id))
        if coupon is None:
            raise ProjectionError(f"预览拒绝：优惠券不存在: {coupon_id}")
        if coupon.used >= coupon.max_uses:
            raise ProjectionError(f"预览拒绝：优惠券已用尽: {coupon_id}")


def _check_refund_order(args: dict[str, Any], shadow: ShadowState) -> None:
    order_id = str(args.get("order_id", ""))
    order = shadow.orders.get(order_id)
    if order is None:
        raise ProjectionError(f"预览拒绝：订单不存在: {order_id}")
    if order.status != "created":
        raise ProjectionError(f"预览拒绝：订单状态不可退款: {order.status}")
