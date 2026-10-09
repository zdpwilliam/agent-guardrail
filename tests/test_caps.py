import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.policy.caps import CapsEvaluationError, evaluate_result_caps
from guardrail.policy.loader import load_policy
from guardrail.projection import ProjectionError
from guardrail.shadow_loader import ShadowLoadError, is_preview_id, load_shadow, project_call
from shop.main import create_app as create_shop_app

POLICY = load_policy("policies/single_call.yaml")


@pytest.fixture
async def shop(tmp_path):
    app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://shop.test") as c:
            yield c


# ---------- 影子装载器 ----------


def test_is_preview_id():
    assert is_preview_id("preview-coupon-0")
    assert is_preview_id("preview-order-3")
    assert not is_preview_id("c-abc")
    assert not is_preview_id("o-abc")
    assert not is_preview_id(0)


async def test_load_shadow_includes_all_products(shop):
    shadow = await load_shadow(shop, [("get_product", {"product_id": "p-iphone"})])
    assert "p-iphone" in shadow.products
    assert shadow.products["p-iphone"].list_price_cents == 599900


async def test_load_shadow_raises_on_missing_coupon(shop):
    with pytest.raises(ShadowLoadError, match="c-nope"):
        await load_shadow(
            shop, [("create_order", {"product_id": "p", "qty": 1, "coupon_id": "c-nope"})]
        )


async def test_load_shadow_skips_intra_plan_ids(shop):
    shadow = await load_shadow(
        shop,
        [
            ("create_coupon", {"code": "S20", "discount_pct": 20.0, "max_uses": 5}),
            (
                "create_order",
                {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"},
            ),
        ],
    )
    assert "p-tshirt-s" in shadow.products


async def test_project_call_applies_effects(shop):
    shadow = await project_call(
        shop, "update_price", {"product_id": "p-iphone", "delta_pct": -10.0}
    )
    assert shadow.products["p-iphone"].list_price_cents == round(599900 * 0.9)


async def test_project_call_rejects_impossible_price(shop):
    with pytest.raises(ProjectionError):
        await project_call(shop, "update_price", {"product_id": "p-iphone", "delta_pct": -100.0})


# ---------- 结果层上限 ----------


async def test_small_order_passes_cap(shop):
    # p-tshirt-s 标价 9900 分，2 件 = 19800 分 < 50000
    assert (
        await evaluate_result_caps(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 2},
            policy=POLICY,
            shop=shop,
        )
        is None
    )


async def test_large_order_hits_cap(shop):
    # p-iphone 599900 分，1 件就超 50000
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-iphone", "qty": 1},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None
    rule_id, reason = hit
    assert rule_id == "cap_order_amount"
    assert "500.00" in reason


async def test_cap_boundary_is_inclusive(shop):
    # p-tshirt-s 标价 9900 分。上限 50000 分。
    # 5 件 = 49500 < 50000 放行；6 件 = 59400 > 50000 拒绝。
    assert (
        await evaluate_result_caps(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 5},
            policy=POLICY,
            shop=shop,
        )
        is None
    )
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-tshirt-s", "qty": 6},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None


async def test_cap_reads_projection_not_args(shop):
    # args 里谎报一个 total_amount 不应该影响判定——上限只看投影结果态。
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-iphone", "qty": 1, "total_amount": 1},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None
    assert hit[0] == "cap_order_amount"


async def test_cap_accounts_for_coupon_discount(shop):
    coupon = (
        await shop.post(
            "/shop/v1/coupons", json={"code": "HALF", "discount_pct": 50.0, "max_uses": 5}
        )
    ).json()
    # p-tshirt-s 9900 → 五折 4950；2 件 = 9900 分，远低于上限。
    assert (
        await evaluate_result_caps(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 2, "coupon_id": coupon["id"]},
            policy=POLICY,
            shop=shop,
        )
        is None
    )


async def test_no_cap_rule_for_tool_means_no_check(shop):
    assert (
        await evaluate_result_caps(
            tool="update_price",
            args={"product_id": "p-iphone", "delta_pct": -1.0},
            policy=POLICY,
            shop=shop,
        )
        is None
    )


async def test_projection_failure_fails_closed(shop):
    with pytest.raises(CapsEvaluationError):
        await evaluate_result_caps(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-does-not-exist"},
            policy=POLICY,
            shop=shop,
        )
