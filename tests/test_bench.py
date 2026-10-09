from guardrail.bench import percentile


def test_percentile_is_monotonic_and_nearest_rank():
    values = [float(i) for i in range(1, 21)]
    assert percentile(values, 0.50) == 10.0
    assert percentile(values, 0.95) == 19.0
    assert percentile(values, 0.99) == 20.0


def test_percentile_single_value():
    assert percentile([3.5], 0.95) == 3.5
