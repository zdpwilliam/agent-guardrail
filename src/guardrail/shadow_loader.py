from __future__ import annotations

from typing import Any

import httpx

from guardrail.models import ShadowState
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import build_shadow, project
from guardrail.projection_args import PlannedAction, derive_projection_args
from guardrail.tools.registry import TOOL_SPECS

_PREVIEW_ID_PREFIX = "preview-"


class ShadowLoadError(Exception):
    """装载影子状态失败。预览映射 400，结果层上限映射 deny。"""


def is_preview_id(value: object) -> bool:
    """是否为计划内部合成的占位 id（`preview-coupon-{step}` / `preview-order-{step}`）。

    真实商城的 id 有各自前缀（coupon 为 `c-…`、order 为 `o-…`），`preview-` 前缀
    只用于投影时给计划内新建实体占位，不可能与真实 id 撞车。装载器据此把
    「计划内引用」与「引用商城已有实体」区分开。
    """
    return str(value).startswith(_PREVIEW_ID_PREFIX)


async def load_referenced_entities(
    shop: httpx.AsyncClient, actions: list[PlannedAction]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把动作 args 引用的 coupon_id / order_id 装进影子状态。

    计划引用了一个商城里真实存在的实体时，影子必须装下它，否则用券下单会被
    投影成不打折、退款会解析不到目标。引用的实体在商城里不存在时直接失败——
    那是一份引用了不存在实体的计划，绝不能让审批人看到一份「看起来没问题」的
    投影终态。

    计划内部新建实体的占位 id（`preview-` 前缀）不在此列：它们由
    `derive_projection_args` 在逐步投影时合成，本函数运行时尚不存在于商城，
    去查只会误报失败。
    """
    coupon_ids: set[str] = set()
    order_ids: set[str] = set()
    for action in actions:
        coupon_id = action.args.get("coupon_id")
        if isinstance(coupon_id, str) and coupon_id and not is_preview_id(coupon_id):
            coupon_ids.add(coupon_id)
        order_id = action.args.get("order_id")
        if order_id and not is_preview_id(order_id):
            order_ids.add(str(order_id))

    coupons: list[dict[str, Any]] = []
    for coupon_id in sorted(coupon_ids):
        resp = await shop.get(f"/shop/v1/coupons/{coupon_id}")
        if resp.status_code == 404:
            raise ShadowLoadError(f"引用的优惠券不存在: {coupon_id}")
        resp.raise_for_status()
        coupons.append(resp.json())

    orders: list[dict[str, Any]] = []
    for order_id in sorted(order_ids):
        resp = await shop.get(f"/shop/v1/orders/{order_id}")
        if resp.status_code == 404:
            raise ShadowLoadError(f"引用的订单不存在: {order_id}")
        resp.raise_for_status()
        orders.append(resp.json())

    return coupons, orders


async def load_shadow(
    shop: httpx.AsyncClient, calls: list[tuple[str, dict[str, Any]]]
) -> ShadowState:
    """为一批调用装载影子状态：全部商品 + 它们引用到的券与订单。"""
    products = (await shop.get("/shop/v1/products")).json()
    actions = [PlannedAction(tool=tool, args=args) for tool, args in calls]
    coupons, orders = await load_referenced_entities(shop, actions)
    return build_shadow(products=products, coupons=coupons, orders=orders)


async def project_call(
    shop: httpx.AsyncClient, tool: str, args: dict[str, Any]
) -> ShadowState:
    """装载影子状态并投影一次调用，返回投影后的状态。

    逐步投影的基准是**当前影子状态**：同一商品在一个序列里被改价多次时，
    第二次的基准必须是第一次投影后的价格，否则投影与真实执行的终态对不上。
    """
    before = await load_shadow(shop, [(tool, args)])
    derived = derive_projection_args(PlannedAction(tool=tool, args=args), before, 0)
    assert_action_applicable(tool, derived, before)
    return project(before, TOOL_SPECS, [(tool, derived)])
