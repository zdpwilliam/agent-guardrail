from guardrail.models import (
    ProductSnapshot,
    ShadowState,
    compute_metrics,
)


def make_state(products):
    return ShadowState(products={p.product_id: p for p in products})


def test_compute_metrics_gross_margin_before_and_after():
    before = make_state(
        [
            ProductSnapshot(
                product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=10
            ),
            ProductSnapshot(
                product_id="p2", cost_price_cents=4000, list_price_cents=10000, stock=10
            ),
        ]
    )
    after = make_state(
        [
            ProductSnapshot(
                product_id="p1", cost_price_cents=6000, list_price_cents=5000, stock=10
            ),
            ProductSnapshot(
                product_id="p2", cost_price_cents=4000, list_price_cents=10000, stock=10
            ),
        ]
    )
    m = compute_metrics(before, after)
    # before: revenue=20000 cost=10000 -> 50.0
    assert m.gross_margin_pct_before == 50.0
    # after:  revenue=15000 cost=10000 -> 33.33
    assert m.gross_margin_pct_after == 33.33
    assert m.affected_product_count == 1
    assert m.avg_price_cents_before == 10000
    assert m.avg_price_cents_after == 7500
    assert m.cash_impact_cents == 0


def test_compute_metrics_no_change_is_identity():
    s = make_state(
        [ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=1)]
    )
    m = compute_metrics(s, s.clone())
    assert m.gross_margin_pct_before == m.gross_margin_pct_after
    assert m.affected_product_count == 0
    assert m.cash_impact_cents == 0


def test_clone_is_deep():
    s = make_state(
        [ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=1)]
    )
    c = s.clone()
    c.products["p1"].list_price_cents = 1
    assert s.products["p1"].list_price_cents == 10000
