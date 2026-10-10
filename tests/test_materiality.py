import pytest

from app import materiality


def test_defaults_for_revenue():
    m = materiality.calculate("revenue", 12_500_000)
    assert m.benchmark_pct == 1.0
    assert m.overall_materiality == 125_000
    assert m.performance_materiality == 93_750
    assert m.trivial_threshold == 6_250


def test_custom_percentages_and_negative_benchmark():
    m = materiality.calculate("pbt", -400_000, benchmark_pct=5, performance_pct=50, trivial_pct=3)
    assert m.overall_materiality == 20_000
    assert m.performance_materiality == 10_000
    assert m.trivial_threshold == 600


@pytest.mark.parametrize("args, message", [
    (("nope", 100), "Unknown benchmark"),
    (("revenue", 0), "must not be zero"),
    (("revenue", 100, 150), "between 0 and 100"),
    (("revenue", 100, 1, 0), "between 0 and 100"),
])
def test_invalid_inputs(args, message):
    with pytest.raises(ValueError, match=message):
        materiality.calculate(*args)
