import pytest

from shop.store import InvalidStateError, NotFoundError, ShopStore


@pytest.fixture
async def store(tmp_path):
    s = ShopStore(str(tmp_path / "shop.db"))
    await s.connect()
    await s.init_schema()
    await s.seed_demo_data()
    yield s
    await s.close()


async def test_update_price_applies_delta(store):
    before = await store.get_product("p-iphone")
    after = await store.update_price("p-iphone", -10.0)
    assert after.list_price_cents == round(before.list_price_cents * 0.9)


async def test_update_price_unknown_product_raises(store):
    with pytest.raises(NotFoundError):
        await store.update_price("p-nope", -10.0)


@pytest.mark.parametrize("delta_pct", [float("nan"), float("inf"), 1e305])
async def test_update_price_rejects_non_finite_delta(store, delta_pct):
    # round(nan) 抛 ValueError、round(inf) 抛 OverflowError；执行必须在 round 前
    # 用有限性守卫拦下，映射成 InvalidStateError（API 层 409），而不是 500。
    with pytest.raises(InvalidStateError):
        await store.update_price("p-iphone", delta_pct)


async def test_update_stock_rejects_negative_result(store):
    with pytest.raises(InvalidStateError):
        await store.update_stock("p-iphone", -99999)


async def test_create_coupon_and_use_it(store):
    coupon = await store.create_coupon("SUMMER20", 20.0, 100)
    product = await store.get_product("p-tshirt-s")
    order = await store.create_order("p-tshirt-s", 2, coupon.id)
    assert order.unit_price_cents == round(product.list_price_cents * 0.8)
    refreshed = await store.get_coupon(coupon.id)
    assert refreshed.used == 1


async def test_coupon_exhausted_raises(store):
    coupon = await store.create_coupon("ONCE", 10.0, 1)
    await store.create_order("p-tshirt-s", 1, coupon.id)
    with pytest.raises(InvalidStateError):
        await store.create_order("p-tshirt-s", 1, coupon.id)


async def test_refund_order_twice_raises(store):
    order = await store.create_order("p-tshirt-s", 1, None)
    await store.refund_order(order.id)
    with pytest.raises(InvalidStateError):
        await store.refund_order(order.id)
