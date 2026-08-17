from scripts.assistant_ttft_benchmark import _metric_summary, _percentile


def test_percentile_uses_nearest_rank() -> None:
    values = [float(value) for value in range(1, 11)]

    assert _percentile(values, 0.50) == 5.0
    assert _percentile(values, 0.95) == 10.0
    assert _percentile([], 0.50) is None


def test_metric_summary_ignores_failed_or_missing_measurements() -> None:
    summary = _metric_summary(
        [{"ttft_seconds": 2.0}, {"ttft_seconds": None}, {"ttft_seconds": 1.0}],
        "ttft_seconds",
    )

    assert summary == {"p50": 1.0, "p95": 2.0, "min": 1.0, "max": 2.0}
