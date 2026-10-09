import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from guardrail.api.tools import PlannedAction, derive_projection_args
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS
from shop.main import create_app
from shop.store import ShopStore
from tests.conftest import app_client

_product_id = st.sampled_from(["p-iphone", "p-ipad", "p-airpods"])
_delta_pct = st.integers(min_value=-50, max_value=50)

# 奇数分商品：标价 9 分。update_price 的两种算术只在「当前价为奇数分、且
# round 的中间值恰好落在 .5」时差一分；现有演示商品的标价全是偶数分
# （599900/439900/189900/…），永远撞不到这个边界，故额外种一个奇数分商品专门触发它。
_ODD_PRODUCT_ID = "p-odd-cents"


@st.composite
def _price_changes(draw):
    """随机改价序列，且每个 example 末尾强制追加一个 .5 边界步骤。

    随机步骤覆盖「同一商品多次改价、第二次以投影后价格为基准」的一般情况；
    末尾固定「奇数分商品 +50%」——此时网关真实派生 round(9 * 1.5) - 9 = 5，
    而旧公式 round(9 * 50 / 100) = 4，两者差一分。若只靠随机采样，这类边界
    几乎不会出现，验收关卡就成了摆设。
    """
    n = draw(st.integers(min_value=0, max_value=5))
    steps = [(draw(_product_id), draw(_delta_pct)) for _ in range(n)]
    steps.append((_ODD_PRODUCT_ID, 50))
    return steps


@hsettings(max_examples=40, deadline=None)
@given(changes=_price_changes())
def test_projection_matches_real_execution(changes):
    """同一组动作，先投影、后真实执行，终态必须一致。

    投影错了，计划审批就是在骗人——这条测试是本计划的验收关卡。

    两点实现约束：
    1. hypothesis 的 @given **不支持 async 测试函数**，所以这里写成同步函数、
       用 asyncio.run 驱动异步逻辑。
    2. 每个 example 必须用**独立的临时数据库**，否则状态会在 example 之间累积，
       价格越跑越偏，测试会以看似随机的方式失败。
    """
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_assert_projection_matches(str(Path(tmp) / "shop.db"), changes))


async def _seed_odd_priced_product(db_path: str) -> None:
    """往测试商城的临时库里额外种一个奇数分商品。

    必须在 create_app 之前种：app 的 lifespan 会 init_schema + seed_demo_data，
    但不会动这个额外商品。只靠 demo 数据的话，影子状态与真实商城都拿不到
    奇数分价格，边界用例无从谈起。
    """
    store = ShopStore(db_path)
    await store.connect()
    await store.init_schema()
    await store.conn.execute(
        "INSERT OR REPLACE INTO products"
        " (id, name, category, cost_price_cents, list_price_cents, stock)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_ODD_PRODUCT_ID, "奇数分测试品", "测试", 5, 9, 10),
    )
    await store.conn.commit()
    await store.close()


async def _assert_projection_matches(db_path: str, changes: list[tuple[str, int]]) -> None:
    await _seed_odd_priced_product(db_path)
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])

        # 1) 逐步投影：走网关真实的派生 + 投影路径（与 POST /v1/plans/preview 一致）
        shadow = before.clone()
        for step, (pid, delta) in enumerate(changes):
            action = PlannedAction(
                tool="update_price", args={"product_id": pid, "delta_pct": delta}
            )
            args = derive_projection_args(action, shadow, step)
            shadow = project(shadow, TOOL_SPECS, [("update_price", args)])

        # 2) 真实执行同一组 delta
        for pid, delta in changes:
            await handle("update_price", {"product_id": pid, "delta_pct": delta}, ctx)

        # 3) 比对终态
        for pid, _ in changes:
            real = (await client.get(f"/shop/v1/products/{pid}")).json()
            assert shadow.products[pid].list_price_cents == real["list_price_cents"], (
                f"{pid} 投影 {shadow.products[pid].list_price_cents} "
                f"≠ 真实 {real['list_price_cents']}；delta 序列 {changes}"
            )


async def test_projection_does_not_mutate_source_state(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as client:
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])
        original = before.products["p-iphone"].list_price_cents
        project(
            before,
            TOOL_SPECS,
            [("update_price", {"product_id": "p-iphone", "absolute_delta_cents": -10000})],
        )
        assert before.products["p-iphone"].list_price_cents == original


# 下单属性测试的商品池（都是 demo 数据里存在的商品）。
_ORDER_PRODUCT_ID = st.sampled_from(["p-tshirt-s", "p-shorts", "p-cap", "p-sandals"])


@st.composite
def _orders(draw):
    """随机下单序列：每单随机选商品与数量，随机决定是否带券。

    用布尔值标记「是否带券」——真实优惠券 id 在运行时才由商城生成，hypothesis
    的策略是同步生成的，拿不到运行时才存在的券 id，故在断言阶段再代入。
    """
    n = draw(st.integers(min_value=1, max_value=6))
    steps = []
    for _ in range(n):
        steps.append(
            (
                draw(_ORDER_PRODUCT_ID),
                draw(st.integers(min_value=1, max_value=5)),
                draw(st.booleans()),
            )
        )
    return steps


@hsettings(max_examples=40, deadline=None)
@given(orders=_orders())
def test_order_projection_matches_real_execution(orders):
    """同一组下单动作，先投影、后真实执行，终态必须一致。

    覆盖 Fix 1 的两处背离：订单**不得**改动 product.stock；带券订单**必须**推进
    coupon.used（否则重复用券的接受/拒绝会预览成成功、执行却 409）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_assert_order_projection_matches(str(Path(tmp) / "shop.db"), orders))


async def _assert_order_projection_matches(db_path: str, orders) -> None:
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        # 先真实创建一张「不限次」券，供带券订单引用（影子与商城各持一份一致快照）。
        coupon = await handle(
            "create_coupon", {"code": "PCT", "discount_pct": 20.0, "max_uses": 1000}, ctx
        )
        coupon_id = coupon["coupon_id"]
        products = (await client.get("/shop/v1/products")).json()
        coupon_row = (await client.get(f"/shop/v1/coupons/{coupon_id}")).json()
        before = build_shadow(products=products, coupons=[coupon_row], orders=[])
        initial_stock = {p["id"]: p["stock"] for p in products}

        # 1) 逐步投影：走网关真实的派生 + 校验 + 投影路径（与 POST /v1/plans/preview 一致）
        shadow = before.clone()
        projected_total = 0
        real_total = 0
        used_orders = 0
        for step, (pid, qty, use_coupon) in enumerate(orders):
            args = {
                "product_id": pid,
                "qty": qty,
                "coupon_id": coupon_id if use_coupon else None,
            }
            action = PlannedAction(tool="create_order", args=args)
            derived = derive_projection_args(action, shadow, step)
            assert_action_applicable("create_order", derived, shadow)
            shadow = project(shadow, TOOL_SPECS, [("create_order", derived)])
            projected_total += shadow.orders[f"preview-order-{step}"].unit_price_cents * qty

            out = await handle(
                "create_order",
                {"product_id": pid, "qty": qty, "coupon_id": coupon_id if use_coupon else None},
                ctx,
            )
            real_total += out["order"]["unit_price_cents"] * qty
            used_orders += 1 if use_coupon else 0

        # 2) 比对终态：库存不被订单改动；券用量按带券订单数推进；现金影响逐分一致。
        for pid in {p[0] for p in orders}:
            real = (await client.get(f"/shop/v1/products/{pid}")).json()
            assert shadow.products[pid].stock == real["stock"] == initial_stock[pid], (
                f"{pid} 投影库存 {shadow.products[pid].stock} ≠ 真实 {real['stock']} "
                f"（初始 {initial_stock[pid]}）；订单序列 {orders}"
            )
        real_coupon = (await client.get(f"/shop/v1/coupons/{coupon_id}")).json()
        assert shadow.coupons[coupon_id].used == real_coupon["used"] == used_orders, (
            f"券用量 投影 {shadow.coupons[coupon_id].used} ≠ 真实 {real_coupon['used']} "
            f"≠ 带券订单数 {used_orders}；订单序列 {orders}"
        )
        assert projected_total == real_total, (
            f"现金影响 投影 {projected_total} ≠ 真实 {real_total}；订单序列 {orders}"
        )


async def test_intra_plan_coupon_double_use_preview_rejects(tmp_path):
    """create_coupon(max_uses=1) → create_order → create_order 必须在预览阶段拒绝第二次下单。

    这是 Fix 1 最危险的接受/拒绝分歧：修复前投影不推进 coupon.used，第二次下单
    预览报成功，真实执行却 409。预览必须与真实执行同判「拒绝」。
    """
    await _assert_intra_plan_coupon_double_use_rejected(str(tmp_path / "shop.db"))


async def _assert_intra_plan_coupon_double_use_rejected(db_path: str) -> None:
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])

        # 1) 预览：create_coupon(max_uses=1) → 两次 create_order(带券)。第二次必须被拒绝。
        shadow = before.clone()
        steps = [
            ("create_coupon", {"code": "ONCE", "discount_pct": 20.0, "max_uses": 1}),
            (
                "create_order",
                {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"},
            ),
            (
                "create_order",
                {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"},
            ),
        ]
        preview_rejected = False
        for step, (tool, raw_args) in enumerate(steps):
            action = PlannedAction(tool=tool, args=raw_args)
            derived = derive_projection_args(action, shadow, step)
            try:
                assert_action_applicable(tool, derived, shadow)
                shadow = project(shadow, TOOL_SPECS, [(tool, derived)])
            except ProjectionError:
                preview_rejected = True
                break
        assert preview_rejected, "预览对 create_coupon → 两次用尽券下单的序列报成功"

        # 2) 真实执行：同一序列，第二次下单必须 409。
        coupon = await handle(
            "create_coupon", {"code": "ONCE", "discount_pct": 20.0, "max_uses": 1}, ctx
        )
        real_coupon_id = coupon["coupon_id"]
        await handle(
            "create_order",
            {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": real_coupon_id},
            ctx,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await handle(
                "create_order",
                {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": real_coupon_id},
                ctx,
            )
