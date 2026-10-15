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

from .exports import finish_workbook

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
MAX_SAMPLE_SIZE = 100_000
MAX_SEED = 2**31 - 1


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
    tolerable, expected = p.tolerable_misstatement, p.expected_misstatement
    if not tolerable or not math.isfinite(tolerable) or tolerable <= 0:
        raise SamplingError("MUS needs a tolerable misstatement (or a sample size).")
    if not math.isfinite(expected) or expected < 0:
        raise SamplingError("Expected misstatement must be a number of zero or more.")
    rf, ef = CONFIDENCE[p.confidence]
    room = p.tolerable_misstatement - p.expected_misstatement * ef
    if room <= 0:
        raise SamplingError(
            "Expected misstatement is too high for the tolerable misstatement; "
            "reduce it or raise tolerable misstatement."
        )
    needed = book_value * rf / room  # can overflow to inf for a tiny tolerable misstatement
    if not math.isfinite(needed) or needed > MAX_SAMPLE_SIZE:
        size = f"{math.ceil(needed):,}" if math.isfinite(needed) else "an unlimited number of"
        raise SamplingError(
            f"That needs a sample of {size} monetary units, over the {MAX_SAMPLE_SIZE:,} limit. "
            "Raise tolerable misstatement or narrow the population."
        )
    return max(1, math.ceil(needed))


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
    if p.seed is not None and not 0 <= p.seed <= MAX_SEED:
        raise SamplingError(f"Seed must be a whole number from 0 to {MAX_SEED:,}.")
    if p.sample_size is not None and not 1 <= p.sample_size <= MAX_SAMPLE_SIZE:
        raise SamplingError(f"Sample size must be from 1 to {MAX_SAMPLE_SIZE:,}.")
    for label, value in (("Minimum amount", p.min_amount),
                         ("Tolerable misstatement", p.tolerable_misstatement),
                         ("Expected misstatement", p.expected_misstatement)):
        # Checked even when a MUS sample size is given, since the values are saved with it.
        if value is not None and not (math.isfinite(value) and value >= 0):
            raise SamplingError(f"{label} must be a number of zero or more.")
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


def to_excel(result: dict, context: dict | None = None, evaluation: dict | None = None) -> bytes:
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
    if evaluation:
        tested = {r["row"]: r for r in evaluation["items"]}
        items["Audited amount"] = [tested[r]["audited_amount"] for r in items["row"]]
        items["Misstatement"] = [tested[r]["misstatement"] for r in items["row"]]
        items["Projected"] = [tested[r]["projected"] for r in items["row"]]
        items["Notes"] = [tested[r]["notes"] for r in items["row"]]
    else:
        for col in ("Tested by", "Date tested", "Result", "Exception amount", "Notes"):
            items[col] = ""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(rows, columns=["Item", "Value"]).to_excel(xl, sheet_name="Parameters",
                                                               index=False)
        items.to_excel(xl, sheet_name="Sample", index=False)
        if evaluation:
            pd.DataFrame(evaluation_rows(evaluation), columns=["Item", "Value"]).to_excel(
                xl, sheet_name="Evaluation", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


# Evaluating a sample once it is tested (ISA 530): the misstatements found in the items are
# projected to the population and compared with tolerable misstatement.

CONCLUSIONS = {
    "incomplete": "Not all items are tested yet",
    "no_tolerable": "Enter a tolerable misstatement to conclude",
    "acceptable": "Below tolerable misstatement: the sample supports the population",
    "close": "Close to tolerable misstatement: consider testing more items",
    "exceeds": "Exceeds tolerable misstatement: extend testing or propose an adjustment",
}


def poisson_upper_limit(errors: int, confidence: int) -> float:
    """The upper limit on the expected number of errors (lambda) at `confidence` %, after
    finding `errors` errors: the lambda at which P(X <= errors) = 1 - confidence."""
    target = 1 - confidence / 100

    def cdf(lam: float) -> float:
        term = total = math.exp(-lam)
        for i in range(1, errors + 1):
            term *= lam / i
            total += term
        return total

    low, high = 0.0, 10.0 + 5.0 * errors
    for _ in range(200):
        mid = (low + high) / 2
        if cdf(mid) > target:
            low = mid
        else:
            high = mid
    return round((low + high) / 2, 4)


def evaluate(result: dict, tested: list[dict], tolerable: float | None) -> dict:
    """Project the misstatements in a tested sample. `tested` has {row, audited_amount,
    notes} for the items tested so far; a misstatement is book value less audited value,
    so overstatements are positive."""
    by_row = {t["row"]: t for t in tested}
    unknown = set(by_row) - {i["row"] for i in result["items"]}
    if unknown:
        raise SamplingError(f"Row {min(unknown)} is not in this sample.")
    if tolerable is not None and not (math.isfinite(tolerable) and tolerable > 0):
        raise SamplingError("Tolerable misstatement must be more than zero.")
    mus = result["method"] == "mus"
    interval = result["details"].get("interval") if mus else None
    items, done = [], []
    for i in result["items"]:
        book = abs(i["amount"])
        t = by_row.get(i["row"], {})
        audited = t.get("audited_amount")
        if audited is not None and not (math.isfinite(audited) and audited >= 0):
            raise SamplingError("Audited amounts must be numbers of zero or more.")
        row = {"row": i["row"], "book": round(book, 2), "audited_amount": audited,
               "notes": t.get("notes", ""), "misstatement": None, "projected": None}
        if audited is not None:
            mis = round(book - audited, 2)
            row["misstatement"] = mis
            if mus and mis:
                if book >= interval:  # top stratum: tested in full, nothing to project
                    row["top_stratum"] = True
                    row["projected"] = mis
                else:
                    row["tainting"] = round(mis / book, 4) if book else 0.0
                    row["projected"] = round(mis / book * interval, 2) if book else 0.0
            done.append(row)
        items.append(row)

    tested_book = sum(r["book"] for r in done)
    exceptions = [r for r in done if r["misstatement"]]
    known = round(sum(r["misstatement"] for r in done), 2)
    out = {
        "items": items, "tested": len(done), "untested": len(items) - len(done),
        "exceptions": len(exceptions),
        "exception_rate": round(100 * len(exceptions) / len(done), 1) if done else None,
        "known_misstatement": known, "tolerable_misstatement": tolerable,
        "population_value": result["population"]["value"],
    }
    if mus:
        confidence = result["params"].get("confidence") or 95
        if confidence not in CONFIDENCE:
            confidence = 95
        limits = [poisson_upper_limit(k, confidence)
                  for k in range(sum(1 for r in exceptions if "tainting" in r) + 1)]
        over = sorted((r["tainting"] for r in exceptions
                       if "tainting" in r and r["tainting"] > 0), reverse=True)
        basic = round(limits[0] * interval, 2)
        allowance = sum((limits[k + 1] - limits[k]) * t * interval for k, t in enumerate(over))
        top = sum(r["misstatement"] for r in exceptions
                  if r.get("top_stratum") and r["misstatement"] > 0)
        out.update(
            basis="mus", confidence=confidence, interval=interval, basic_precision=basic,
            projected_misstatement=round(sum(r["projected"] for r in exceptions), 2),
            upper_limit=round(basic + allowance + top, 2),
        )
        measure = out["upper_limit"]
    else:
        # Over- and understatements are projected separately, so errors in opposite
        # directions can't cancel out; the conclusion rests on the larger of the two.
        scale = result["population"]["value"] / tested_book if tested_book else 0.0
        over = sum(r["misstatement"] for r in exceptions if r["misstatement"] > 0)
        under = sum(-r["misstatement"] for r in exceptions if r["misstatement"] < 0)
        out.update(basis="ratio",
                   projected_misstatement=round(known * scale, 2),
                   projected_overstatement=round(over * scale, 2),
                   projected_understatement=round(under * scale, 2),
                   upper_limit=None)
        measure = max(out["projected_overstatement"], out["projected_understatement"])
    if out["untested"]:
        status = "incomplete"
    elif not tolerable:
        status = "no_tolerable"
    elif measure > tolerable:
        status = "exceeds"
    elif not mus and measure > tolerable / 2:
        status = "close"
    else:
        status = "acceptable"
    out["conclusion"] = {"status": status, "label": CONCLUSIONS[status]}
    return out


def evaluation_rows(ev: dict) -> list[tuple]:
    rows = [("Items tested", f"{ev['tested']} of {ev['tested'] + ev['untested']}"),
            ("Items with a misstatement", ev["exceptions"]),
            ("Known misstatement in the sample", ev["known_misstatement"])]
    if ev["basis"] == "mus":
        rows += [("Basis", "Monetary unit sampling (Poisson upper limits)"),
                 ("Confidence %", ev["confidence"]), ("Sampling interval", ev["interval"]),
                 ("Basic precision", ev["basic_precision"]),
                 ("Projected misstatement", ev["projected_misstatement"]),
                 ("Upper misstatement limit (overstatement)", ev["upper_limit"])]
    else:
        rows += [("Basis", "Ratio of misstatement to the value tested, applied to the "
                           "population (non-statistical)"),
                 ("Projected overstatement", ev["projected_overstatement"]),
                 ("Projected understatement", ev["projected_understatement"]),
                 ("Projected misstatement (net)", ev["projected_misstatement"])]
    rows += [("Tolerable misstatement", ev["tolerable_misstatement"]),
             ("Conclusion", ev["conclusion"]["label"])]
    return rows
