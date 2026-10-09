import pytest

from shop.main import create_app
from tests.conftest import app_client


@pytest.fixture
async def client(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as c:
        yield c


async def test_list_products(client):
    r = await client.get("/shop/v1/products")
    assert r.status_code == 200
    assert len(r.json()) >= 6


async def test_shop_health_and_readiness(client):
    assert (await client.get("/healthz")).status_code == 200
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}


async def test_shop_sqlite_uses_wal_and_busy_timeout(client):
    store = client._transport.app.state.store
    journal = await store.conn.execute("PRAGMA journal_mode")
    timeout = await store.conn.execute("PRAGMA busy_timeout")
    journal_row = await journal.fetchone()
    timeout_row = await timeout.fetchone()
    assert journal_row[0].lower() == "wal"
    assert timeout_row[0] >= 5000


async def test_get_product_404(client):
    r = await client.get("/shop/v1/products/p-nope")
    assert r.status_code == 404


async def test_update_price_roundtrip(client):
    before = (await client.get("/shop/v1/products/p-iphone")).json()
    r = await client.post("/shop/v1/products/p-iphone/price", json={"delta_pct": -10.0})
    assert r.status_code == 200
    assert r.json()["list_price_cents"] == round(before["list_price_cents"] * 0.9)


async def test_stock_negative_returns_409(client):
    r = await client.post("/shop/v1/products/p-iphone/stock", json={"delta": -99999})
    assert r.status_code == 409


@pytest.mark.parametrize("delta_pct", ["nan", "inf", 1e305])
async def test_update_price_non_finite_returns_409(client, delta_pct):
    r = await client.post(
        "/shop/v1/products/p-iphone/price", json={"delta_pct": delta_pct}
    )
    assert r.status_code == 409


async def test_coupon_and_order_flow(client):
    c = await client.post(
        "/shop/v1/coupons", json={"code": "S20", "discount_pct": 20.0, "max_uses": 5}
    )
    assert c.status_code == 200
    o = await client.post(
        "/shop/v1/orders",
        json={"product_id": "p-tshirt-s", "qty": 2, "coupon_id": c.json()["id"]},
    )
    assert o.status_code == 200
    assert o.json()["status"] == "created"


async def test_get_coupon_200(client):
    c = await client.post(
        "/shop/v1/coupons", json={"code": "GETME", "discount_pct": 15.0, "max_uses": 3}
    )
    cid = c.json()["id"]
    r = await client.get(f"/shop/v1/coupons/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == cid
    assert body["code"] == "GETME"
    assert body["discount_pct"] == 15.0


async def test_get_coupon_404(client):
    r = await client.get("/shop/v1/coupons/c-nope")
    assert r.status_code == 404


async def test_refund_twice_returns_409(client):
    o = await client.post(
        "/shop/v1/orders", json={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": None}
    )
    oid = o.json()["id"]
    assert (await client.post(f"/shop/v1/orders/{oid}/refund")).status_code == 200
    assert (await client.post(f"/shop/v1/orders/{oid}/refund")).status_code == 409
