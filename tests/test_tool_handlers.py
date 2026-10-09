import pytest

from guardrail.tools.handlers import ToolContext, handle
from shop.main import create_app
from tests.conftest import app_client


@pytest.fixture
async def ctx(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as c:
        yield ToolContext(shop=c)


async def test_list_products(ctx):
    out = await handle("list_products", {}, ctx)
    assert len(out["products"]) >= 6


async def test_get_product(ctx):
    out = await handle("get_product", {"product_id": "p-iphone"}, ctx)
    assert out["product"]["id"] == "p-iphone"


async def test_update_price_returns_absolute_delta(ctx):
    out = await handle("update_price", {"product_id": "p-iphone", "delta_pct": -10.0}, ctx)
    assert out["before"]["list_price_cents"] == 599900
    assert out["absolute_delta_cents"] == out["after"]["list_price_cents"] - 599900


async def test_create_order_server_fills_unit_price_and_order_id(ctx):
    out = await handle("create_order", {"product_id": "p-tshirt-s", "qty": 2}, ctx)
    assert out["order"]["unit_price_cents"] > 0
    assert out["order_id"].startswith("o-")


async def test_create_coupon_returns_coupon_id(ctx):
    out = await handle(
        "create_coupon", {"code": "T20", "discount_pct": 20.0, "max_uses": 3}, ctx
    )
    assert out["coupon_id"].startswith("c-")


async def test_send_email_reports_domain(ctx):
    out = await handle("send_email", {"to": "a@external.example"}, ctx)
    assert out["to_domain"] == "external.example"


async def test_unknown_tool_raises(ctx):
    with pytest.raises(KeyError):
        await handle("nope", {}, ctx)
