"""Materiality calculator: overall, performance and clearly trivial thresholds.

The percentages are common starting points from practice, not requirements of any
standard; the auditor picks the benchmark and percentage and records why.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

BENCHMARKS: dict[str, dict] = {
    "pbt": {"label": "Profit before tax", "default_pct": 5.0, "range": "3–10%"},
    "revenue": {"label": "Revenue", "default_pct": 1.0, "range": "0.5–2%"},
    "total_assets": {"label": "Total assets", "default_pct": 1.0, "range": "0.5–2%"},
    "equity": {"label": "Equity", "default_pct": 2.0, "range": "1–5%"},
    "expenses": {"label": "Total expenses", "default_pct": 1.0, "range": "0.5–2%"},
}
DEFAULT_PERFORMANCE_PCT = 75.0  # of overall materiality (typically 50–75%)
DEFAULT_TRIVIAL_PCT = 5.0  # of overall materiality (typically 3–5%)


@dataclass
class Materiality:
    benchmark: str
    benchmark_amount: float
    benchmark_pct: float
    performance_pct: float
    trivial_pct: float
    overall_materiality: float
    performance_materiality: float
    trivial_threshold: float

    def to_dict(self) -> dict:
        return asdict(self)


def calculate(benchmark: str, benchmark_amount: float, benchmark_pct: float | None = None,
              performance_pct: float | None = None,
              trivial_pct: float | None = None) -> Materiality:
    if benchmark not in BENCHMARKS:
        raise ValueError(f"Unknown benchmark {benchmark!r}.")
    amount = abs(float(benchmark_amount))
    if not math.isfinite(amount):
        raise ValueError("Benchmark amount must be a number.")
    if amount == 0:
        raise ValueError("Benchmark amount must not be zero.")
    pct = BENCHMARKS[benchmark]["default_pct"] if benchmark_pct is None else benchmark_pct
    perf = DEFAULT_PERFORMANCE_PCT if performance_pct is None else performance_pct
    triv = DEFAULT_TRIVIAL_PCT if trivial_pct is None else trivial_pct
    for name, value in (("Benchmark percentage", pct), ("Performance percentage", perf),
                        ("Trivial percentage", triv)):
        if not 0 < value <= 100:
            raise ValueError(f"{name} must be between 0 and 100.")
    overall = round(amount * pct / 100, 2)
    return Materiality(
        benchmark=benchmark,
        benchmark_amount=amount,
        benchmark_pct=pct,
        performance_pct=perf,
        trivial_pct=triv,
        overall_materiality=overall,
        performance_materiality=round(overall * perf / 100, 2),
        trivial_threshold=round(overall * triv / 100, 2),
    )
