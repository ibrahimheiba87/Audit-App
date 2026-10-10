"""Audit sampling: random, systematic and monetary unit sampling (MUS), reproducible by seed.

MUS sample sizes use the zero-expected-error reliability factors from the Poisson
distribution and AICPA-style expansion factors for expected misstatement. They are a
starting point; firms often have their own tables.
"""

from __future__ import annotations

import io
import math
import secrets
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

METHODS = {
    "random": "Simple random",
    "systematic": "Systematic (fixed interval)",
    "mus": "Monetary unit (MUS)",
}
# Confidence level (%) -> reliability factor for zero errors, expansion factor.
CONFIDENCE = {
    80: (1.61, 1.3),
    85: (1.90, 1.4),
    90: (2.31, 1.5),
    95: (3.00, 1.6),
    99: (4.61, 1.9),
}
POPULATIONS = {"all": "All lines", "debits": "Debits only", "credits": "Credits only"}


class SamplingError(ValueError):
    pass


@dataclass
class SampleParams:
    method: str
    sample_size: int | None = None  # required for random/systematic, optional for MUS
    seed: int | None = None
    population: str = "all"
    min_amount: float = 0.0
    confidence: int = 95  # MUS only
    tolerable_misstatement: float | None = None  # MUS only
    expected_misstatement: float = 0.0  # MUS only

    def to_dict(self) -> dict:
        return asdict(self)


def select_population(df: pd.DataFrame, p: SampleParams) -> pd.DataFrame:
    if p.population not in POPULATIONS:
        raise SamplingError(f"Population must be one of: {', '.join(POPULATIONS)}.")
    pop = df
    if p.population == "debits":
        pop = pop[pop["amount"] > 0]
    elif p.population == "credits":
        pop = pop[pop["amount"] < 0]
    if p.min_amount:
        pop = pop[pop["amount"].abs() >= p.min_amount]
    pop = pop[pop["amount"].abs() > 0]
    if pop.empty:
        raise SamplingError("No lines are left in the population after the filters.")
    return pop.reset_index(drop=True)


def mus_sample_size(book_value: float, p: SampleParams) -> int:
    if p.confidence not in CONFIDENCE:
        raise SamplingError(f"Confidence must be one of: {', '.join(map(str, CONFIDENCE))}.")
    if not p.tolerable_misstatement or p.tolerable_misstatement <= 0:
        raise SamplingError("MUS needs a tolerable misstatement (or a sample size).")
    rf, ef = CONFIDENCE[p.confidence]
    room = p.tolerable_misstatement - p.expected_misstatement * ef
    if room <= 0:
        raise SamplingError(
            "Expected misstatement is too high for the tolerable misstatement; "
            "reduce it or raise tolerable misstatement."
        )
    return max(1, math.ceil(book_value * rf / room))


def _item(row: pd.Series, reason: str) -> dict:
    def get(col):
        value = row.get(col)
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        return value

    return {
        "row": int(row["row"]), "entry_id": get("entry_id"), "date": get("date"),
        "account": get("account"), "description": get("description"),
        "amount": round(float(row["amount"]), 2), "reason": reason,
    }


def draw(df: pd.DataFrame, p: SampleParams) -> dict:
    if p.method not in METHODS:
        raise SamplingError(f"Method must be one of: {', '.join(METHODS)}.")
    seed = p.seed if p.seed is not None else secrets.randbelow(2**31)
    rng = np.random.default_rng(seed)
    pop = select_population(df, p)
    n_pop = len(pop)
    values = pop["amount"].abs().to_numpy()
    book_value = float(values.sum())
    details: dict = {}

    if p.method in ("random", "systematic"):
        if not p.sample_size or p.sample_size < 1:
            raise SamplingError("Enter a sample size of at least 1.")
        n = min(p.sample_size, n_pop)
        if p.method == "random":
            picks = sorted(rng.choice(n_pop, size=n, replace=False).tolist())
            items = [_item(pop.iloc[i], "Random selection") for i in picks]
        else:
            interval = n_pop / n
            start = float(rng.uniform(0, interval))
            picks = [int(start + i * interval) for i in range(n)]
            items = [_item(pop.iloc[i], f"Item {i + 1} of population") for i in picks]
            details = {"interval": round(interval, 4), "start": round(start, 4)}
        if p.sample_size > n_pop:
            details["note"] = f"Sample size capped at the population of {n_pop} lines."
    else:
        n = p.sample_size or mus_sample_size(book_value, p)
        interval = book_value / n
        start = float(rng.uniform(0, interval))
        cumulative = np.cumsum(values)
        hits_at = start + interval * np.arange(n)
        indices = np.searchsorted(cumulative, hits_at, side="right")
        indices = np.minimum(indices, n_pop - 1)
        hits = pd.Series(indices).value_counts().sort_index()
        items = []
        for i, count in hits.items():
            reason = ("Top stratum: at or above the sampling interval" if values[i] >= interval
                      else "Monetary unit selection")
            item = _item(pop.iloc[int(i)], reason)
            item["hits"] = int(count)
            items.append(item)
        details = {
            "interval": round(interval, 2), "start": round(start, 2), "monetary_units": n,
            "top_stratum_items": int((values >= interval).sum()),
        }
        if not p.sample_size:
            rf, ef = CONFIDENCE[p.confidence]
            details["reliability_factor"] = rf
            details["expansion_factor"] = ef

    return {
        "method": p.method,
        "method_label": METHODS[p.method],
        "params": {**p.to_dict(), "seed": seed},
        "seed": seed,
        "population": {
            "label": POPULATIONS[p.population], "lines": n_pop,
            "value": round(book_value, 2),
        },
        "details": details,
        "sample_size": len(items),
        "sample_value": round(sum(abs(i["amount"]) for i in items), 2),
        "items": items,
    }


def to_excel(result: dict, context: dict | None = None) -> bytes:
    params = result["params"]
    rows = list((context or {}).items()) + [
        ("Method", result["method_label"]),
        ("Seed", result["seed"]),
        ("Population", result["population"]["label"]),
        ("Population lines", result["population"]["lines"]),
        ("Population value", result["population"]["value"]),
        ("Minimum amount", params["min_amount"]),
        ("Items selected", result["sample_size"]),
        ("Value selected", result["sample_value"]),
    ]
    if result["method"] == "mus":
        rows += [("Confidence %", params["confidence"]),
                 ("Tolerable misstatement", params["tolerable_misstatement"]),
                 ("Expected misstatement", params["expected_misstatement"])]
    rows += [(k.replace("_", " ").capitalize(), v) for k, v in result["details"].items()]
    rows.append(("Reproduce", "Use the same file, filters, method, size and seed."))
    items = pd.DataFrame(result["items"])
    for col in ("Tested by", "Date tested", "Result", "Exception amount", "Notes"):
        items[col] = ""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(rows, columns=["Item", "Value"]).to_excel(xl, sheet_name="Parameters",
                                                               index=False)
        items.to_excel(xl, sheet_name="Sample", index=False)
        for ws in xl.book.worksheets:
            for column in ws.columns:
                width = max(len(str(c.value or "")) for c in column)
                ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
    return buf.getvalue()
