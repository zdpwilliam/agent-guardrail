from guardrail.api.tools import PlannedAction, derive_projection_args
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.tools.registry import TOOL_SPECS
from shop.main import create_app
from tests.conftest import app_client


def _simple_case(tool, args):
    async def prepare(client):
        return tool, args, [], []

    return prepare


async def _coupon_accept(client):
    c = (await client.post(
        "/shop/v1/coupons", json={"code": "OK", "discount_pct": 20.0, "max_uses": 2}
    )).json()
    return (
        "create_order",
        {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": c["id"]},
        [c["id"]],
        [],
    )


async def _coupon_exhausted(client):
    c = (await client.post(
        "/shop/v1/coupons", json={"code": "EX", "discount_pct": 20.0, "max_uses": 1}
    )).json()
    await client.post(
        "/shop/v1/orders", json={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": c["id"]}
    )
    return (
        "create_order",
        {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": c["id"]},
        [c["id"]],
        [],
    )


async def _refund_accept(client):
    o = (await client.post(
        "/shop/v1/orders", json={"product_id": "p-tshirt-s", "qty": 1}
    )).json()
    return "refund_order", {"order_id": o["id"]}, [], [o["id"]]


async def _refund_refunded(client):
    o = (await client.post(
        "/shop/v1/orders", json={"product_id": "p-tshirt-s", "qty": 1}
    )).json()
    await client.post(f"/shop/v1/orders/{o['id']}/refund")
    return "refund_order", {"order_id": o["id"]}, [], [o["id"]]


CASES = [
    (
        "update_price accept",
        _simple_case("update_price", {"product_id": "p-iphone", "delta_pct": -10.0}),
    ),
    (
        "update_price reject non-positive",
        _simple_case("update_price", {"product_id": "p-iphone", "delta_pct": -100.0}),
    ),
    (
        "update_stock accept",
        _simple_case("update_stock", {"product_id": "p-iphone", "delta": -5}),
    ),
    (
        "update_stock reject negative",
        _simple_case("update_stock", {"product_id": "p-iphone", "delta": -99999}),
    ),
    (
        "create_coupon accept",
        _simple_case("create_coupon", {"code": "OK", "discount_pct": 20.0, "max_uses": 5}),
    ),
    (
        "create_coupon reject zero",
        _simple_case("create_coupon", {"code": "Z", "discount_pct": 0.0, "max_uses": 5}),
    ),
    (
        "create_coupon reject hundred",
        _simple_case("create_coupon", {"code": "H", "discount_pct": 100.0, "max_uses": 5}),
    ),
    (
        "create_order accept",
        _simple_case("create_order", {"product_id": "p-tshirt-s", "qty": 2}),
    ),
    (
        "create_order reject qty zero",
        _simple_case("create_order", {"product_id": "p-tshirt-s", "qty": 0}),
    ),
    (
        "create_order reject non-integral qty 1.5",
        _simple_case("create_order", {"product_id": "p-tshirt-s", "qty": 1.5}),
    ),
    (
        "create_order reject non-integral qty 2.9",
        _simple_case("create_order", {"product_id": "p-tshirt-s", "qty": 2.9}),
    ),
    (
        "create_coupon reject non-string code null",
        _simple_case("create_coupon", {"code": None, "discount_pct": 20.0, "max_uses": 5}),
    ),
    (
        "create_coupon reject non-string code int",
        _simple_case("create_coupon", {"code": 0, "discount_pct": 20.0, "max_uses": 5}),
    ),
    (
        "create_coupon reject non-string code dict",
        _simple_case("create_coupon", {"code": {}, "discount_pct": 20.0, "max_uses": 5}),
    ),
    (
        "create_coupon reject non-integral max_uses",
        _simple_case("create_coupon", {"code": "OK", "discount_pct": 20.0, "max_uses": 1.5}),
    ),
    (
        "create_order reject missing product",
        _simple_case("create_order", {"product_id": "p-nope", "qty": 1}),
    ),
    (
        "create_order reject missing coupon",
        _simple_case(
            "create_order", {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-nope"}
        ),
    ),
    ("create_order accept with coupon", _coupon_accept),
    ("create_order reject exhausted coupon", _coupon_exhausted),
    ("refund_order accept", _refund_accept),
    (
        "refund_order reject missing order",
        _simple_case("refund_order", {"order_id": "o-nope"}),
    ),
    ("refund_order reject non-created status", _refund_refunded),
]


async def _gateway_ok(tool, args, shadow) -> bool:
    try:
        derived = derive_projection_args(PlannedAction(tool=tool, args=args), shadow, 0)
        assert_action_applicable(tool, derived, shadow)
        project(shadow, TOOL_SPECS, [(tool, derived)])
        return True
    except ProjectionError:
        return False


async def _shop_ok(client, tool, args) -> bool:
    if tool == "update_price":
        r = await client.post(
            f"/shop/v1/products/{args['product_id']}/price",
            json={"delta_pct": args["delta_pct"]},
        )
    elif tool == "update_stock":
        r = await client.post(
            f"/shop/v1/products/{args['product_id']}/stock", json={"delta": args["delta"]}
        )
    elif tool == "create_coupon":
        r = await client.post(
            "/shop/v1/coupons",
            json={
                "code": args["code"],
                "discount_pct": args["discount_pct"],
                "max_uses": args["max_uses"],
            },
        )
    elif tool == "create_order":
        r = await client.post(
            "/shop/v1/orders",
            json={
                "product_id": args["product_id"],
                "qty": args["qty"],
                "coupon_id": args.get("coupon_id"),
            },
        )
    elif tool == "refund_order":
        r = await client.post(f"/shop/v1/orders/{args['order_id']}/refund")
    else:
        raise AssertionError(f"unexpected tool: {tool}")
    return r.status_code < 400


async def _snapshot(client, coupon_ids, order_ids):
    products = (await client.get("/shop/v1/products")).json()
    coupons = [(await client.get(f"/shop/v1/coupons/{cid}")).json() for cid in coupon_ids]
    orders = [(await client.get(f"/shop/v1/orders/{oid}")).json() for oid in order_ids]
    return build_shadow(products=products, coupons=coupons, orders=orders)


async def test_preview_rules_parity(tmp_path):
    for i, (name, prepare) in enumerate(CASES):
        async with app_client(create_app(str(tmp_path / f"shop-{i}.db"))) as client:
            tool, args, coupon_ids, order_ids = await prepare(client)
            shadow = await _snapshot(client, coupon_ids, order_ids)
            gateway_ok = await _gateway_ok(tool, args, shadow)
            shop_ok = await _shop_ok(client, tool, args)
            assert gateway_ok == shop_ok, (
                f"{name}: 网关 accept={gateway_ok}, 商城 accept={shop_ok}"
            )
