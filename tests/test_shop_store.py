import pytest

from shop.store import ShopStore


@pytest.fixture
async def store(tmp_path):
    s = ShopStore(str(tmp_path / "shop.db"))
    await s.connect()
    await s.init_schema()
    yield s
    await s.close()


async def test_init_schema_creates_tables(store):
    rows = await store._fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    assert {"products", "orders", "coupons"} <= {r["name"] for r in rows}


async def test_money_columns_are_integer_cents(store):
    cols = await store._fetchall("PRAGMA table_info(products)")
    types = {c["name"]: c["type"] for c in cols}
    assert types["cost_price_cents"] == "INTEGER"
    assert types["list_price_cents"] == "INTEGER"


async def test_seed_and_list_products(store):
    await store.seed_demo_data()
    products = await store.list_products()
    assert len(products) >= 6
    p = await store.get_product(products[0].id)
    assert p is not None and p.cost_price_cents > 0


async def test_list_products_by_category(store):
    await store.seed_demo_data()
    summer = await store.list_products(category="夏季款")
    assert len(summer) >= 4
    assert all(p.category == "夏季款" for p in summer)
