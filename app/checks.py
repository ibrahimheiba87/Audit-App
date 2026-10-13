"""Audit checks run against a normalized ledger DataFrame (see loader.normalize)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

HIGH, MEDIUM, LOW = "high", "medium", "low"

# Weekend choices, as pandas day numbers (Monday is 0). Egypt and much of the Gulf rest on
# Friday and Saturday.
WEEKENDS: dict[str, dict] = {
    "sat-sun": {"label": "Saturday and Sunday", "days": (5, 6)},
    "fri-sat": {"label": "Friday and Saturday", "days": (4, 5)},
    "fri": {"label": "Friday only", "days": (4,)},
}

# Words in a description that often mark a manual or unusual entry, in English and Arabic.
DEFAULT_KEYWORDS = (
    "adjust", "reverse", "reversal", "correct", "error", "write off", "write-off", "plug",
    "suspense", "manual", "override", "reclass",
    "تسوية", "عكس", "تصحيح", "خطأ", "شطب", "إلغاء",
)
MAX_KEYWORDS = 50


def parse_keywords(text: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Keywords from a comma, Arabic comma or newline separated list; None gives the defaults."""
    if text is None:
        return DEFAULT_KEYWORDS
    parts = re.split(r"[,،\n]", text) if isinstance(text, str) else text
    out: list[str] = []
    for part in parts:
        word = " ".join(str(part).split())[:50]
        if word and word.casefold() not in {w.casefold() for w in out}:
            out.append(word)
    return tuple(out[:MAX_KEYWORDS])


@dataclass
class Settings:
    period_end: date | None = None  # inferred from the data when not given
    cutoff_days: int = 5
    round_threshold: float = 1000.0
    outlier_iqr_multiplier: float = 1.5
    balance_tolerance: float = 0.01
    performance_materiality: float | None = None
    trivial_threshold: float | None = None
    weekend: str = "sat-sun"  # a key of WEEKENDS
    keywords: tuple[str, ...] | list[str] = DEFAULT_KEYWORDS
    work_start: int = 7  # entries recorded before this hour or from work_end on are flagged
    work_end: int = 19
    rare_account_lines: int = 2  # accounts with this many lines or fewer are "rarely used"
    kind: str | None = None  # the upload's kind ("gl", "tb", "prior_tb") when known


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
    if "date" not in keys or len(keys) < 3:
        # Without a date, repeated balances (e.g. on a trial balance) aren't duplicates.
        res.status, res.note = "skipped", "Needs a date column and an account or description column."
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
    rest = amounts % t  # float remainders: 3.3 % 1.1 is 1.0999…, so allow half a cent either way
    flagged = df[(amounts >= t) & ((rest < 0.005) | (t - rest < 0.005))]
    for _, r in flagged.iterrows():
        res.findings.append(_finding(res.id, MEDIUM, r, f"Exact multiple of {_money(t)}"))
    return res


def check_weekend(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("weekend", "Weekend entries")
    col = "posted" if "posted" in df else "date" if "date" in df else None
    if col is None:
        res.status, res.note = "skipped", "Needs a date column."
        return res
    weekend = WEEKENDS.get(s.weekend, WEEKENDS["sat-sun"])
    res.note = (f"Weekend is {weekend['label']}; based on the "
                f"{'posting' if col == 'posted' else 'transaction'} date.")
    dates = df[col]
    flagged = df[dates.notna() & dates.dt.dayofweek.isin(weekend["days"])]
    for _, r in flagged.iterrows():
        res.findings.append(_finding(res.id, MEDIUM, r, f"Recorded on a {r[col]:%A}"))
    return res


def _timed(dates: pd.Series) -> pd.Series:
    """True where a date carries a time of day. A date given without one reads as midnight,
    so midnight exactly counts as no time."""
    return dates.notna() & (dates != dates.dt.normalize())


def _time_column(df: pd.DataFrame) -> str | None:
    """The posting or transaction date column, if any of its dates carry times of day."""
    for col in ("posted", "date"):
        if col in df and _timed(df[col]).any():
            return col
    return None


def check_after_hours(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("after_hours", "Outside working hours")
    col = _time_column(df)
    if col is None:
        res.status, res.note = "skipped", "Needs dates with times of day."
        return res
    res.note = (f"Recorded before {s.work_start:02d}:00 or from {s.work_end:02d}:00, by the "
                f"{'posting' if col == 'posted' else 'transaction'} time.")
    times = df[col]
    hours = times.dt.hour
    flagged = df[_timed(times) & ((hours < s.work_start) | (hours >= s.work_end))]
    for _, r in flagged.iterrows():
        res.findings.append(_finding(res.id, MEDIUM, r, f"Recorded at {r[col]:%H:%M}"))
    return res


def _keyword_pattern(word: str) -> str:
    # English words match at the start of a word, so "adjust" finds "adjustment" but not
    # "readjust"; Arabic words take attached prefixes (و، ال), so they match anywhere.
    # (?<!\w) rather than \b, so a keyword starting with "#" or "(" still matches.
    return rf"(?<!\w){re.escape(word)}" if word.isascii() else re.escape(word)


def check_keywords(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("keywords", "Keywords in descriptions")
    if "description" not in df:
        res.status, res.note = "skipped", "Needs a description column."
        return res
    words = list(s.keywords)
    if not words:
        res.status, res.note = "skipped", "No keywords set."
        return res
    res.note = "Looking for: " + ", ".join(words) + "."
    text = df["description"].astype("string").fillna("")
    hits = {w: text.str.contains(_keyword_pattern(w), case=False, regex=True) for w in words}
    any_hit = pd.concat(hits.values(), axis=1).any(axis=1)
    for i, r in df[any_hit].iterrows():
        found = ", ".join(w for w in words if hits[w].loc[i])
        res.findings.append(_finding(res.id, MEDIUM, r, f'Description contains "{found}"'))
    return res


def check_rare_accounts(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("rare_accounts", "Rarely used accounts")
    if "account" not in df:
        res.status, res.note = "skipped", "Needs an account column."
        return res
    counts = df["account"].value_counts(dropna=True)
    # A trial balance has one line per account, so every account would look rare. Without
    # the upload's kind (a quick audit), guess from the file's shape.
    if s.kind is not None:
        is_tb = s.kind != "gl"
    else:
        is_tb = len(df) < 50 or counts.mean() < 5
    if is_tb or not len(counts):
        res.status, res.note = "skipped", "Needs a general ledger with several lines per account."
        return res
    n = s.rare_account_lines
    rare = counts[counts <= n]
    res.note = f"Accounts with {n} lines or fewer in this file ({len(rare)} accounts)."
    for _, r in df[df["account"].isin(rare.index)].iterrows():
        used = int(counts[r["account"]])
        res.findings.append(_finding(
            res.id, LOW, r, f"Account used on only {used} line{'s' if used != 1 else ''}"))
    return res


MAX_GAP_FINDINGS = 200


def check_gaps(df: pd.DataFrame, s: Settings) -> CheckResult:
    """Missing numbers in the journal entry sequence (JE-1001, JE-1002, JE-1004 → JE-1003).

    Numbers are compared within each prefix. A jump bigger than the number of entries
    (JE1430 to JE9999) starts a new block rather than counting as missing entries, and a
    block with more missing numbers than used ones is not a running sequence, so it is
    left out.
    """
    res = CheckResult("gaps", "Gaps in entry numbers")
    if "entry_id" not in df:
        res.status, res.note = "skipped", "Needs an entry number column."
        return res
    ids = pd.Series(df["entry_id"].dropna().astype(str).str.strip().unique(), dtype="string")
    parts = ids.str.extract(r"^(.*?)(\d{1,15})$").dropna()
    notes, numbered = [], False
    for prefix, g in parts.groupby(0, sort=True):
        if len(g) < 10:
            continue
        numbered = True
        width = int(g[1].str.len().min()) if g[1].str.startswith("0").any() else 0
        nums = np.unique(g[1].astype("int64").to_numpy())

        def name(n: int, prefix: str = prefix, width: int = width) -> str:
            return f"{prefix}{n:0{width}d}"

        breaks = np.flatnonzero(np.diff(nums) > len(nums)) + 1
        for block in np.split(nums, breaks):
            if len(block) < 2:
                continue
            steps = np.diff(block)
            missing = int((steps - 1).sum())
            label = f"{name(block[0])}–{name(block[-1])}"
            if missing > len(block):
                notes.append(f"{label} is not a running sequence")
                continue
            notes.append(f"{label}: {missing} missing")
            for at in np.flatnonzero(steps > 1):
                first, last = int(block[at]) + 1, int(block[at + 1]) - 1
                count = last - first + 1
                res.findings.append({
                    "check": res.id, "severity": MEDIUM, "row": None,
                    "entry_id": name(first) + (f"–{name(last)}" if last > first else ""),
                    "date": None, "account": None, "description": None, "amount": 0.0,
                    "detail": f"{count} missing entry number{'s' if count != 1 else ''}",
                })
    if not numbered:
        res.status, res.note = "skipped", "Needs at least 10 numbered entries."
        return res
    if not notes:
        res.note = "Entry numbers are not a running sequence."
        return res
    if len(res.findings) > MAX_GAP_FINDINGS:
        notes.append(f"showing the first {MAX_GAP_FINDINGS} of {len(res.findings)} gaps")
        res.findings = res.findings[:MAX_GAP_FINDINGS]
    res.note = "; ".join(notes) + "."
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


def check_materiality(df: pd.DataFrame, s: Settings) -> CheckResult:
    res = CheckResult("materiality", "At or above performance materiality")
    pm = s.performance_materiality
    if not pm:
        res.status, res.note = "skipped", "Set performance materiality to run this check."
        return res
    res.note = f"Lines of {_money(pm)} or more (performance materiality)."
    for _, r in df[df["amount"].abs() >= pm].iterrows():
        res.findings.append(_finding(
            res.id, HIGH, r, f"At or above performance materiality of {_money(pm)}",
        ))
    return res


SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

# Risk score points for failing a check, by severity, with a few checks weighted on their own.
SEVERITY_POINTS = {HIGH: 20, MEDIUM: 10, LOW: 5}
CHECK_POINTS = {"keywords": 15, "outliers": 15, "benford": 3}
SIZE_POINTS = 20  # the most an entry's size adds
RISK_LEVELS = ((50, HIGH), (25, MEDIUM), (0, LOW))
MAX_RANKED = 5000  # kept with the run; a run can flag far more lines than anyone reviews


def risk_ranking(df: pd.DataFrame, findings: list[dict], s: Settings) -> dict:
    """Score each journal entry (or line, without entry numbers) from 0 to 100.

    An entry earns points once for each check it fails, so an entry that is posted at the
    weekend, in a round amount, to a rarely used account scores above one that only fails
    one test. Its size adds up to SIZE_POINTS more: against performance materiality when it
    is set, otherwise by how it ranks among the file's amounts.
    """
    by_entry = "entry_id" in df and df["entry_id"].notna().any()
    keys = (df["entry_id"].astype("string").fillna("row " + df["row"].astype(str))
            if by_entry else "row " + df["row"].astype(str))
    key_of_row = dict(zip(df["row"], keys, strict=True))

    points: dict[str, dict[str, int]] = {}
    for f in findings:
        if f["row"] is None or f["row"] not in key_of_row:
            continue
        p = CHECK_POINTS.get(f["check"], SEVERITY_POINTS[f["severity"]])
        checks_hit = points.setdefault(key_of_row[f["row"]], {})
        checks_hit[f["check"]] = max(checks_hit.get(f["check"], 0), p)

    flagged = df[keys.isin(points)]
    groups = flagged.groupby(keys[flagged.index], sort=False)
    pm = s.performance_materiality
    sizes = df["amount"].abs()
    ranked = []
    for key, g in groups:
        debits = g.loc[g["amount"] > 0, "amount"].sum()
        size = float(debits if debits else g["amount"].abs().sum())
        if pm:
            share = min(size / pm, 1.0)
        else:
            share = float((sizes <= size).mean())
        hit = points[key]
        score = min(100, round(sum(hit.values()) + SIZE_POINTS * share))
        first = g.iloc[0]
        accounts = g["account"].dropna().unique() if "account" in g else []
        descriptions = g["description"].dropna() if "description" in g else []
        when = first.get("date")
        entry_id = first.get("entry_id")
        if entry_id is not None and pd.isna(entry_id):
            entry_id = None
        elif hasattr(entry_id, "item"):  # a numpy number from a column of numeric codes
            entry_id = entry_id.item()
        ranked.append({
            "score": score,
            "level": next(level for floor, level in RISK_LEVELS if score >= floor),
            "entry_id": entry_id,
            "row": int(first["row"]),
            "lines": int(len(g)),
            "date": when.date().isoformat() if isinstance(when, pd.Timestamp) else None,
            "account": str(accounts[0]) if len(accounts) == 1 else None,
            "accounts": int(len(accounts)),
            "description": str(descriptions.iloc[0]) if len(descriptions) else None,
            "amount": round(size, 2),
            "reasons": sorted(hit, key=lambda c: -hit[c]),
        })
    ranked.sort(key=lambda e: (-e["score"], -e["amount"], e["row"]))
    for i, e in enumerate(ranked, 1):
        e["rank"] = i
    return {
        "basis": "entry" if by_entry else "line",
        "scored": len(ranked),
        "by_level": {lvl: sum(1 for e in ranked if e["level"] == lvl) for _, lvl in RISK_LEVELS},
        "size_basis": "performance materiality" if pm else "file amounts",
        "entries": ranked[:MAX_RANKED],
    }


def run_all(df: pd.DataFrame, settings: Settings | None = None,
            import_report: dict | None = None) -> dict:
    s = settings or Settings()
    analysis = benford(df)
    results = [
        check_balance(df, s),
        check_duplicates(df, s),
        check_materiality(df, s),
        check_cutoff(df, s),
        check_weekend(df, s),
        check_after_hours(df, s),
        check_keywords(df, s),
        check_round_amounts(df, s),
        check_outliers(df, s),
        check_rare_accounts(df, s),
        check_gaps(df, s),
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
        "materiality": {
            "performance_materiality": s.performance_materiality,
            "trivial_threshold": s.trivial_threshold,
            "findings_above_trivial": sum(
                1 for f in findings
                if s.trivial_threshold and abs(f["amount"]) > s.trivial_threshold
            ),
        },
        "import": import_report,
        "checks": [r.summary() for r in results],
        "findings": findings,
        "risk": risk_ranking(df, findings, s),
        "benford": analysis,
    }
