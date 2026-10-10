"""Audit checks run against a normalized ledger DataFrame (see loader.normalize)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass
class Settings:
    period_end: date | None = None  # inferred from the data when not given
    cutoff_days: int = 5
    round_threshold: float = 1000.0
    outlier_iqr_multiplier: float = 1.5
    balance_tolerance: float = 0.01


@dataclass
class CheckResult:
    id: str
    name: str
    status: str = "ran"  # "ran" or "skipped"
    note: str = ""
    findings: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "flagged": len(self.findings),
            "note": self.note,
        }


def _finding(check: str, severity: str, row: pd.Series, detail: str) -> dict:
    def get(col):
        value = row.get(col)
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        return value

    return {
        "check": check,
        "severity": severity,
        "row": int(row["row"]),
        "entry_id": get("entry_id"),
        "date": get("date"),
        "account": get("account"),
        "description": get("description"),
        "amount": round(float(row["amount"]), 2),
        "detail": detail,
    }


def _money(x: float) -> str:
    return f"{x:,.2f}"


def check_duplicates(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("duplicates", "Duplicate entries")
    keys = [c for c in ("date", "account", "description") if c in df] + ["amount"]
    if keys == ["amount"]:
        res.status, res.note = "skipped", "Needs a date, account or description column."
        return res
    dupes = df[df.duplicated(subset=keys, keep=False)]
    res.note = "Rows matching on " + ", ".join(keys) + "."
    for _, group in dupes.groupby(keys, dropna=False, sort=False):
        for _, r in group.iterrows():
            others = [str(o) for o in group["row"] if o != r["row"]]
            label = "row" if len(others) == 1 else "rows"
            res.findings.append(_finding(res.id, HIGH, r, f"Matches {label} {', '.join(others)}"))
    return res


def _benford_amounts(df: pd.DataFrame) -> pd.Series:
    positive = df["amount"][df["amount"] > 0]
    # A file of only credits (or a credit-signed export) still gets tested.
    return positive if len(positive) else df["amount"].abs()


BENFORD_EXPECTED = {d: math.log10(1 + 1 / d) for d in range(1, 10)}


def benford(df: pd.DataFrame) -> dict:
    """First-digit Benford analysis on debit amounts of 10 or more.

    Only debits are tested so each balanced entry is counted once rather than twice.
    """
    amounts = _benford_amounts(df)
    amounts = amounts[amounts >= 10]
    n = int(len(amounts))
    digits = amounts.astype(str).str.lstrip("0.").str[0].astype(int)
    counts = digits.value_counts().reindex(range(1, 10), fill_value=0)

    rows = []
    mad = 0.0
    for d in range(1, 10):
        expected = BENFORD_EXPECTED[d]
        actual = counts[d] / n if n else 0.0
        mad += abs(actual - expected)
        # Z-statistic with continuity correction (Nigrini).
        z = 0.0
        if n:
            se = math.sqrt(expected * (1 - expected) / n)
            z = max(abs(actual - expected) - 1 / (2 * n), 0) / se
        rows.append({
            "digit": d,
            "count": int(counts[d]),
            "actual": round(actual, 4),
            "expected": round(expected, 4),
            "z": round(z, 2),
            "significant": bool(z > 1.96 and actual > expected),
        })
    mad = mad / 9

    # Nigrini's first-digit MAD conformity ranges.
    if mad <= 0.006:
        conformity = "Close conformity"
    elif mad <= 0.012:
        conformity = "Acceptable conformity"
    elif mad <= 0.015:
        conformity = "Marginally acceptable conformity"
    else:
        conformity = "Nonconformity"

    return {"n": n, "mad": round(mad, 4), "conformity": conformity, "digits": rows}


def check_benford(df: pd.DataFrame, s: Settings, analysis: dict) -> CheckResult:
    res = CheckResult("benford", "Benford's law (first digit)")
    if analysis["n"] < 100:
        res.status = "skipped"
        res.note = f"Only {analysis['n']} debit amounts of 10 or more; Benford needs at least 100."
        return res
    res.note = f"{analysis['conformity']} (MAD {analysis['mad']:.4f}, n={analysis['n']})."
    if analysis["conformity"] != "Nonconformity":
        return res
    spikes = {r["digit"]: r for r in analysis["digits"] if r["significant"]}
    if not spikes:
        return res
    amounts = _benford_amounts(df)
    amounts = amounts[amounts >= 10]
    first = amounts.astype(str).str.lstrip("0.").str[0]
    for d, info in spikes.items():
        for _, r in df.loc[first[first == str(d)].index].iterrows():
            res.findings.append(_finding(
                res.id, LOW, r,
                f"Leading digit {d} appears {info['actual']:.1%} of the time vs "
                f"{info['expected']:.1%} expected",
            ))
    return res


def check_round_amounts(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("round_amounts", "Round-number amounts")
    t = s.round_threshold
    res.note = f"Amounts of {_money(t)} or more that are exact multiples of {_money(t)}."
    amounts = df["amount"].abs().round(2)
    flagged = df[(amounts >= t) & ((amounts % t) == 0)]
    for _, r in flagged.iterrows():
        res.findings.append(_finding(res.id, MEDIUM, r, f"Exact multiple of {_money(t)}"))
    return res


def check_weekend(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("weekend", "Weekend entries")
    col = "posted" if "posted" in df else "date" if "date" in df else None
    if col is None:
        res.status, res.note = "skipped", "Needs a date column."
        return res
    res.note = f"Based on the {'posting' if col == 'posted' else 'transaction'} date."
    dates = df[col]
    flagged = df[dates.notna() & (dates.dt.dayofweek >= 5)]
    for _, r in flagged.iterrows():
        res.findings.append(_finding(res.id, MEDIUM, r, f"Recorded on a {r[col]:%A}"))
    return res


def infer_period_end(df: pd.DataFrame) -> date | None:
    if "date" not in df or df["date"].isna().all():
        return None
    latest = df["date"].max()
    return (latest + pd.offsets.MonthEnd(0)).date()


def check_cutoff(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("cutoff", "Period cutoff")
    if "date" not in df:
        res.status, res.note = "skipped", "Needs a date column."
        return res
    period_end = s.period_end or infer_period_end(df)
    if period_end is None:
        res.status, res.note = "skipped", "No valid dates found."
        return res
    end = pd.Timestamp(period_end)
    window = pd.Timedelta(days=s.cutoff_days)
    inferred = "" if s.period_end else " (inferred from the latest date)"
    res.note = (f"Period end {period_end.isoformat()}{inferred}, flagging entries in the last "
                f"{s.cutoff_days} days or later.")

    for _, r in df.iterrows():
        d = r["date"]
        if pd.isna(d):
            continue
        posted = r.get("posted")
        if posted is not None and not pd.isna(posted) and d <= end < posted:
            res.findings.append(_finding(
                res.id, HIGH, r,
                f"Dated in the period but posted after period end ({posted:%Y-%m-%d})",
            ))
        elif d > end:
            res.findings.append(_finding(res.id, HIGH, r, "Dated after period end"))
        elif end - window < d <= end:
            res.findings.append(_finding(
                res.id, LOW, r, f"Within {s.cutoff_days} days before period end",
            ))
    return res


def check_outliers(df: pd.DataFrame, s: Settings) -> CheckResult:
    """Flag amounts far above what is typical for their account.

    Ledger amounts are heavily right-skewed, so the fence is computed on log10 of the
    absolute amounts. Accounts with fewer than 10 lines use the whole file's fence.
    """
    res = CheckResult("outliers", "Unusually large amounts")
    k = s.outlier_iqr_multiplier
    res.note = f"Amounts above Q3 + {k:g}×IQR of their account's amounts, on a log scale."
    nonzero = df[df["amount"].abs() > 0]
    logs = np.log10(nonzero["amount"].abs())

    def fence(values: pd.Series) -> float:
        q1, q3 = values.quantile([0.25, 0.75])
        return float(10 ** (q3 + k * (q3 - q1)))

    file_limit = fence(logs) if len(logs) else math.inf
    groups = nonzero.groupby("account", dropna=False) if "account" in df else [(None, nonzero)]
    for _, g in groups:
        if len(g) >= 10:
            limit, base = fence(logs[g.index]), "account"
        else:
            limit, base = file_limit, "file"
        for _, r in g[g["amount"].abs() > limit].iterrows():
            res.findings.append(_finding(
                res.id, MEDIUM, r, f"Above the {base} threshold of {_money(limit)}",
            ))
    return res


def check_balance(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("balance", "Debits equal credits")
    tol = s.balance_tolerance
    total = float(df["amount"].sum())
    notes = []
    if abs(total) > tol:
        notes.append(f"File is out of balance by {_money(total)}.")
        res.findings.append({
            "check": res.id, "severity": HIGH, "row": None, "entry_id": None, "date": None,
            "account": None, "description": None, "amount": round(total, 2),
            "detail": f"Total debits and credits differ by {_money(total)}",
        })
    else:
        notes.append("File balances.")
    if "entry_id" in df:
        sums = df.groupby("entry_id")["amount"].sum()
        bad = sums[sums.abs() > tol]
        for entry, diff in bad.items():
            first = df[df["entry_id"] == entry].iloc[0]
            res.findings.append(_finding(
                res.id, HIGH, first, f"Entry {entry} is out of balance by {_money(diff)}",
            ))
        notes.append(f"{len(bad)} unbalanced journal entries.")
    res.note = " ".join(notes)
    return res


SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}


def run_all(df: pd.DataFrame, settings: Settings | None = None,
            import_report: dict | None = None) -> dict:
    s = settings or Settings()
    analysis = benford(df)
    results = [
        check_balance(df, s),
        check_duplicates(df, s),
        check_cutoff(df, s),
        check_weekend(df, s),
        check_round_amounts(df, s),
        check_outliers(df, s),
        check_benford(df, s, analysis),
    ]
    findings = [f for r in results for f in r.findings]
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["row"] or 0))
    flagged_rows = {f["row"] for f in findings if f["row"] is not None}

    return {
        "summary": {
            "rows": int(len(df)),
            "columns": [c for c in df.columns if c != "row"],
            "total_debits": round(float(df.loc[df["amount"] > 0, "amount"].sum()), 2),
            "total_credits": round(float(-df.loc[df["amount"] < 0, "amount"].sum()), 2),
            "flagged_rows": len(flagged_rows),
            "findings_by_severity": {
                sev: sum(1 for f in findings if f["severity"] == sev) for sev in (HIGH, MEDIUM, LOW)
            },
        },
        "import": import_report,
        "checks": [r.summary() for r in results],
        "findings": findings,
        "benford": analysis,
    }
