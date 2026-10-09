import pytest

from guardrail.projection import build_shadow
from guardrail.projection_args import PlannedAction, derive_projection_args

SHADOW = build_shadow(
    products=[
        {
            "id": "p-tshirt-s",
            "name": "T",
            "category": "夏季款",
            "cost_price_cents": 3500,
            "list_price_cents": 9900,
            "stock": 300,
        }
    ],
    coupons=[],
    orders=[],
)


def _shadow_with_price(price_cents: int):
    return build_shadow(
        products=[
            {
                "id": "p",
                "name": "X",
                "category": "c",
                "cost_price_cents": 1,
                "list_price_cents": price_cents,
                "stock": 10,
            }
        ],
        coupons=[],
        orders=[],
    )


def test_update_price_derives_absolute_delta_from_shadow_price():
    out = derive_projection_args(
        PlannedAction(tool="update_price", args={"product_id": "p-tshirt-s", "delta_pct": -10.0}),
        SHADOW,
        0,
    )
    assert out["absolute_delta_cents"] == -990


@pytest.mark.parametrize(
    ("before", "delta_pct", "expected"),
    [
        # 回归用例（任务记录的 counterexample）：round(9 * 1.5) = 14 → delta 5，
        # 旧公式 round(9 * 0.5) = 4 会少一分。
        (9, 50.0, 5),
        # 其余 .5 边界：round(11 * 1.5) = 16 → delta 5。
        (11, 50.0, 5),
        # 正数 .5：round(5 * 1.1) = 6 → delta 1。
        (5, 10.0, 1),
        # 负数 .5：round(5 * 0.9) = 4 → delta -1。
        (5, -10.0, -1),
        # 非边界 sanity：与旧公式一致，确保常规路径不受影响。
        (9900, -10.0, -990),
    ],
)
def test_update_price_delta_matches_shop_rounding(before, delta_pct, expected):
    out = derive_projection_args(
        PlannedAction(tool="update_price", args={"product_id": "p", "delta_pct": delta_pct}),
        _shadow_with_price(before),
        0,
    )
    assert out["absolute_delta_cents"] == expected
    # 关键不变量：before + delta 必须逐分复现商城的存储值。
    assert before + out["absolute_delta_cents"] == round(before * (1 + delta_pct / 100))


def test_update_price_missing_product_injects_no_field():
    out = derive_projection_args(
        PlannedAction(tool="update_price", args={"product_id": "p-nope", "delta_pct": -10.0}),
        SHADOW,
        0,
    )
    assert "absolute_delta_cents" not in out


def test_create_coupon_synthesizes_deterministic_coupon_id():
    out = derive_projection_args(
        PlannedAction(
            tool="create_coupon", args={"code": "S20", "discount_pct": 20.0, "max_uses": 5}
        ),
        SHADOW,
        2,
    )
    assert out["coupon_id"] == "preview-coupon-2"


def test_create_order_synthesizes_id_coupon_and_unit_price():
    out = derive_projection_args(
        PlannedAction(tool="create_order", args={"product_id": "p-tshirt-s", "qty": 3}),
        SHADOW,
        1,
    )
    assert out["order_id"] == "preview-order-1"
    # 未带券时 coupon_id 显式写成 None，供 optional 的券用量递增效果判定「不动券」。
    assert out["coupon_id"] is None
    assert out["unit_price_cents"] == 9900


def test_create_order_unknown_coupon_leaves_price_undiscounted():
    out = derive_projection_args(
        PlannedAction(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-not-in-shadow"},
        ),
        SHADOW,
        0,
    )
    assert out["unit_price_cents"] == 9900


def test_refund_order_keeps_real_order_id_from_args():
    out = derive_projection_args(
        PlannedAction(tool="refund_order", args={"order_id": "o-real"}), SHADOW, 0
    )
    assert out["order_id"] == "o-real"


def test_read_and_noop_tools_pass_args_through_unchanged():
    out = derive_projection_args(
        PlannedAction(tool="send_email", args={"to": "a@b.test"}), SHADOW, 0
    )
    assert out == {"to": "a@b.test"}
