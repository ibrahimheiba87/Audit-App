"""Stratification, summaries and field statistics for a ledger (IDEA-style analysis).

Stratification splits the lines into bands by the size of their amount (debit or credit),
so the auditor can see where the value sits before choosing what to test. Summaries total
the lines by account, user and month. Field statistics describe each column: how many
values are filled, blank and distinct, with totals for amounts and the range of dates.
"""

from __future__ import annotations

import io
import math

import pandas as pd

from .exports import finish_workbook
from .loader import DATE_FIELDS, FIELDS

MAX_BANDS = 20
MAX_ROWS = 500  # summary rows sent to the page; the Excel download has them all
LABELS = {f["id"]: f["label"] for f in FIELDS}
SUMMARIES = {"account": "By account", "user": "By user", "month": "By month"}


class StratifyError(ValueError):
    pass


def default_bands(max_abs: float, materiality: dict | None = None) -> list[float]:
    """Band floors: the engagement's trivial, performance and overall materiality when set,
    otherwise powers of ten up to the largest amount."""
    m = materiality or {}
    levels = [m.get(k) for k in ("trivial_threshold", "performance_materiality",
                                 "overall_materiality")]
    levels = sorted({round(float(v), 2) for v in levels if v and math.isfinite(v) and v > 0})
    if levels:
        return [0.0, *levels]
    bands = [0.0]
    step = 100.0
    while step <= max_abs and len(bands) < 8:
        bands.append(step)
        step *= 10
    return bands


def check_bands(bands: list[float]) -> list[float]:
    if not bands:
        raise StratifyError("Enter at least one band.")
    if any(not math.isfinite(b) or b < 0 for b in bands):
        raise StratifyError("Bands must be amounts of zero or more.")
    out = sorted(set(round(float(b), 2) for b in bands))
    out = out if out[0] == 0 else [0.0, *out]
    if len(out) > MAX_BANDS:  # counted with the zero band, so the result can be sent back
        raise StratifyError(f"Use at most {MAX_BANDS} bands.")
    return out


def _sides(amount: pd.Series) -> tuple[pd.Series, pd.Series]:
    return amount.clip(lower=0), (-amount).clip(lower=0)


def stratify(df: pd.DataFrame, bands: list[float]) -> list[dict]:
    amount = df["amount"].astype(float)
    size = amount.abs()
    debit, credit = _sides(amount)
    total_lines, total_value = len(df), float(size.sum())
    out = []
    for i, floor in enumerate(bands):
        ceiling = bands[i + 1] if i + 1 < len(bands) else None
        mask = size >= floor
        if ceiling is not None:
            mask &= size < ceiling
        value = float(size[mask].sum())
        out.append({
            "from": floor, "to": ceiling, "lines": int(mask.sum()),
            "debits": round(float(debit[mask].sum()), 2),
            "credits": round(float(credit[mask].sum()), 2),
            "value": round(value, 2),
            "lines_pct": round(100 * int(mask.sum()) / total_lines, 1) if total_lines else 0.0,
            "value_pct": round(100 * value / total_value, 1) if total_value else 0.0,
        })
    return out


def summarize(df: pd.DataFrame, by: str) -> list[dict]:
    """Lines, debits, credits, net and the largest line for each value of `by`."""
    if by == "month":
        if "date" not in df:
            return []
        dates = pd.to_datetime(df["date"], errors="coerce")
        keys = dates.dt.strftime("%Y-%m").fillna("(no date)")
    else:
        if by not in df:
            return []
        keys = df[by].map(lambda v: "(blank)" if v is None or (not isinstance(v, str)
                                                              and pd.isna(v)) or str(v).strip() == ""
                          else str(v))
    amount = df["amount"].astype(float)
    debit, credit = _sides(amount)
    frame = pd.DataFrame({"key": keys.to_numpy(), "amount": amount.to_numpy(),
                          "debit": debit.to_numpy(), "credit": credit.to_numpy(),
                          "size": amount.abs().to_numpy()})
    grouped = frame.groupby("key", sort=False)
    largest = frame.loc[grouped["size"].idxmax(), ["key", "amount"]].set_index("key")["amount"]
    table = grouped.agg(lines=("amount", "size"), debits=("debit", "sum"),
                        credits=("credit", "sum"), net=("amount", "sum"))
    table["largest"] = largest
    table["turnover"] = table["debits"] + table["credits"]
    table = table.sort_index() if by == "month" else table.sort_values("turnover",
                                                                       ascending=False)
    return [{"key": str(k), "lines": int(r.lines), "debits": round(float(r.debits), 2),
             "credits": round(float(r.credits), 2), "net": round(float(r.net), 2),
             "largest": round(float(r.largest), 2)} for k, r in table.iterrows()]


def _plain(value):
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item"):  # numpy scalar
        return value.item()
    return value


def field_stats(df: pd.DataFrame) -> list[dict]:
    out = []
    for col in df.columns:
        if col == "row":
            continue
        s = df[col]
        blank = s.isna() | s.map(lambda v: isinstance(v, str) and not v.strip())
        filled = s[~blank]
        stat = {"field": col, "label": LABELS.get(col, col.replace("_", " ").capitalize()),
                "filled": int(len(filled)), "blank": int(blank.sum()),
                "distinct": int(filled.astype(str).nunique())}
        if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
            nums = pd.to_numeric(filled, errors="coerce").dropna()
            if len(nums):
                stat.update(kind="number", total=round(float(nums.sum()), 2),
                            min=round(float(nums.min()), 2), max=round(float(nums.max()), 2),
                            average=round(float(nums.mean()), 2),
                            zeros=int((nums == 0).sum()), negatives=int((nums < 0).sum()))
        elif col in DATE_FIELDS or pd.api.types.is_datetime64_any_dtype(s):
            dates = pd.to_datetime(filled, errors="coerce").dropna()
            if len(dates):
                stat.update(kind="date", earliest=_plain(dates.min()), latest=_plain(dates.max()))
        else:
            counts = filled.astype(str).value_counts()
            if len(counts):
                stat.update(kind="text", most_common=str(counts.index[0]),
                            most_common_lines=int(counts.iloc[0]))
        out.append(stat)
    return out


def analyze(df: pd.DataFrame, bands: list[float] | None = None,
            materiality: dict | None = None) -> dict:
    if "amount" not in df or df.empty:
        raise StratifyError("This file has no amounts to analyse.")
    max_abs = float(df["amount"].abs().max())
    bands = check_bands(bands) if bands else default_bands(max_abs, materiality)
    summaries = {}
    for by in SUMMARIES:
        rows = summarize(df, by)
        if rows:
            summaries[by] = {"label": SUMMARIES[by], "count": len(rows), "rows": rows}
    debit, credit = _sides(df["amount"].astype(float))
    return {
        "bands": bands,
        "totals": {"lines": len(df), "debits": round(float(debit.sum()), 2),
                   "credits": round(float(credit.sum()), 2),
                   "value": round(float(df["amount"].abs().sum()), 2)},
        "strata": stratify(df, bands),
        "summaries": summaries,
        "fields": field_stats(df),
    }


def trimmed(result: dict) -> dict:
    """The result with each summary cut to MAX_ROWS rows, for the page."""
    out = {**result, "summaries": {}}
    for by, s in result["summaries"].items():
        out["summaries"][by] = {**s, "rows": s["rows"][:MAX_ROWS],
                                "truncated": max(0, s["count"] - MAX_ROWS)}
    return out


def band_label(stratum: dict) -> str:
    if stratum["to"] is None:
        return f"{stratum['from']:,.2f} and above"
    return f"{stratum['from']:,.2f} to under {stratum['to']:,.2f}"


def to_excel(result: dict, context: dict | None = None) -> bytes:
    about = pd.DataFrame(list((context or {}).items()) + [
        ("Lines", result["totals"]["lines"]), ("Debits", result["totals"]["debits"]),
        ("Credits", result["totals"]["credits"]),
        ("Bands (from)", ", ".join(f"{b:,.2f}" for b in result["bands"])),
    ], columns=["Item", "Value"])
    strata = pd.DataFrame([{
        "Band": band_label(s), "Lines": s["lines"], "% of lines": s["lines_pct"],
        "Debits": s["debits"], "Credits": s["credits"], "Value (debits + credits)": s["value"],
        "% of value": s["value_pct"]} for s in result["strata"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        strata.to_excel(xl, sheet_name="Stratification", index=False)
        for by, s in result["summaries"].items():
            first = {"account": "Account", "user": "User", "month": "Month"}[by]
            pd.DataFrame([{first: r["key"], "Lines": r["lines"], "Debits": r["debits"],
                           "Credits": r["credits"], "Net": r["net"],
                           "Largest line": r["largest"]} for r in s["rows"]]).to_excel(
                xl, sheet_name=s["label"], index=False)
        pd.DataFrame([{
            "Field": f["label"], "Filled": f["filled"], "Blank": f["blank"],
            "Distinct": f["distinct"], "Total": f.get("total"), "Min": f.get("min"),
            "Max": f.get("max"), "Average": f.get("average"), "Earliest": f.get("earliest"),
            "Latest": f.get("latest"), "Most common": f.get("most_common"),
            "Most common lines": f.get("most_common_lines")} for f in result["fields"]]).to_excel(
            xl, sheet_name="Field statistics", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()
