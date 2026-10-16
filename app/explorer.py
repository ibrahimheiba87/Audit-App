"""The ledger explorer: filter a general ledger (or any file with amounts) and pivot it.

The auditor narrows the lines by date, account, description, user, entry, amount, debit or
credit side and weekend postings, sees the matching lines with their totals, and turns them
into a pivot: rows by account, user, month or entry, optionally split into columns by month,
user or account. Nothing is saved; each look is logged, and the result downloads to Excel.
It only reads, so it works on locked files too.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import current_user, log
from .db import Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_LINES = 500     # lines sent to the page; the Excel download has up to MAX_EXPORT
MAX_EXPORT = 100_000
MAX_PIVOT_ROWS = 300
MAX_PIVOT_COLUMNS = 36
GROUPS = {"account": "Account", "user": "User", "month": "Month", "entry_id": "Entry"}
SPLITS = {"none": "Nothing", "month": "Month", "user": "User", "account": "Account"}
SORTS = {"date": "Date", "largest": "Largest amount first"}
Side = Literal["any", "debit", "credit"]


class ExploreIn(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    account: str = Field(default="", max_length=200)      # contains, any case
    text: str = Field(default="", max_length=200)         # in the description
    user: str = Field(default="", max_length=200)
    entry_id: str = Field(default="", max_length=200)
    min_amount: float | None = Field(default=None, ge=0)  # by size, debit or credit
    max_amount: float | None = Field(default=None, ge=0)
    side: Side = "any"
    weekend: bool = False
    rows: Literal["account", "user", "month", "entry_id"] = "account"
    columns: Literal["none", "month", "user", "account"] = "none"
    sort: Literal["date", "largest"] = "date"


def money(v) -> float:
    return round(float(v), 2) + 0.0  # + 0.0 turns -0.0 (from an empty sum) into 0.0


def text_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df:
        return pd.Series([""] * len(df), index=df.index, dtype="object")
    return df[col].astype("object").where(df[col].notna(), "").astype(str)


def months(df: pd.DataFrame) -> pd.Series:
    if "date" not in df:
        return pd.Series(["(no date)"] * len(df), index=df.index)
    return df["date"].dt.strftime("%Y-%m").fillna("(no date)")


def key(df: pd.DataFrame, by: str) -> pd.Series:
    if by == "month":
        return months(df)
    out = text_col(df, by).str.strip()
    return out.where(out != "", "(blank)")


def apply(df: pd.DataFrame, f: ExploreIn) -> pd.DataFrame:
    """The lines that pass every filter."""
    if f.date_from and f.date_to and f.date_from > f.date_to:
        raise HTTPException(400, "The start date is after the end date.")
    if f.min_amount is not None and f.max_amount is not None and f.min_amount > f.max_amount:
        raise HTTPException(400, "The smallest amount is more than the largest.")
    keep = pd.Series(True, index=df.index)
    if f.date_from or f.date_to or f.weekend:
        if "date" not in df:
            raise HTTPException(400, "This file has no dates to filter on.")
        d = df["date"]
        if f.date_from:
            keep &= d >= pd.Timestamp(f.date_from)
        if f.date_to:
            keep &= d < pd.Timestamp(f.date_to) + pd.Timedelta(days=1)
        if f.weekend:
            keep &= d.dt.dayofweek >= 5
    for col, needle in (("account", f.account), ("description", f.text), ("user", f.user),
                        ("entry_id", f.entry_id)):
        if needle.strip():
            if col not in df:
                raise HTTPException(400, f"This file has no {col.replace('_id', '')} column "
                                         "to filter on.")
            keep &= text_col(df, col).str.contains(needle.strip(), case=False, regex=False)
    size = df["amount"].abs()
    if f.min_amount is not None:
        keep &= size >= f.min_amount
    if f.max_amount is not None:
        keep &= size <= f.max_amount
    if f.side == "debit":
        keep &= df["amount"] > 0
    elif f.side == "credit":
        keep &= df["amount"] < 0
    return df[keep]


def pivot(df: pd.DataFrame, rows: str, columns: str) -> dict:
    """Net amounts by `rows` (and `columns`), with a count and total for each row."""
    if columns == rows:
        columns = "none"
    frame = pd.DataFrame({"row": key(df, rows), "amount": df["amount"]})
    if columns != "none":
        frame["col"] = key(df, columns)
    totals = frame.groupby("row").agg(lines=("amount", "size"), net=("amount", "sum"),
                                      debits=("amount", lambda a: a[a > 0].sum()),
                                      credits=("amount", lambda a: -a[a < 0].sum()))
    totals = totals.sort_index() if rows == "month" else \
        totals.assign(turnover=totals.debits + totals.credits).sort_values(
            "turnover", ascending=False).drop(columns="turnover")
    cols, cells = [], {}
    if columns != "none":
        table = frame.pivot_table(index="row", columns="col", values="amount", aggfunc="sum",
                                  fill_value=0.0)
        cols = sorted(table.columns) if columns == "month" else \
            list(table.abs().sum().sort_values(ascending=False).index)
        cols = cols[:MAX_PIVOT_COLUMNS]
        cells = {r: {c: money(table.at[r, c]) for c in cols} for r in table.index}
    out = [{"key": str(r), "lines": int(t.lines), "debits": money(t.debits),
            "credits": money(t.credits), "net": money(t.net),
            "cells": [cells[r][c] for c in cols] if cols else []}
           for r, t in totals.iterrows()]
    return {"rows_by": rows, "columns_by": columns, "columns": [str(c) for c in cols],
            "columns_total": int(frame["col"].nunique()) if columns != "none" else 0,
            "rows": out, "rows_total": len(out)}


def line_out(r) -> dict:
    day = r.get("date")
    return {"row": int(r["row"]),
            "date": day.date().isoformat() if isinstance(day, pd.Timestamp) else None,
            **{c: "" if pd.isna(r.get(c)) else str(r.get(c))
               for c in ("account", "entry_id", "description", "user")},
            "amount": money(r["amount"])}


def explore(upload: Upload, f: ExploreIn) -> tuple[pd.DataFrame, dict]:
    df = load_upload(upload).df
    hits = apply(df, f)
    if f.sort == "largest":
        hits = hits.reindex(hits["amount"].abs().sort_values(ascending=False).index)
    elif "date" in hits:
        hits = hits.sort_values(["date", "row"], na_position="last")
    amount = hits["amount"]
    result = {
        "upload_id": upload.id, "filename": upload.filename, "lines_in_file": len(df),
        "matched": len(hits), "debits": money(amount[amount > 0].sum()),
        "credits": money(-amount[amount < 0].sum()), "net": money(amount.sum()),
        "has": [c for c in ("date", "account", "entry_id", "description", "user") if c in df],
        "pivot": pivot(hits, f.rows, f.columns),
    }
    return hits, result


def trimmed(hits: pd.DataFrame, result: dict) -> dict:
    p = result["pivot"]
    return {**result, "lines": [line_out(r) for _, r in hits.head(MAX_LINES).iterrows()],
            "pivot": {**p, "rows": p["rows"][:MAX_PIVOT_ROWS]}}


def filters_text(f: ExploreIn) -> list[tuple[str, str]]:
    out = [(label, str(v)) for label, v in (
        ("From", f.date_from), ("To", f.date_to), ("Account contains", f.account),
        ("Description contains", f.text), ("User contains", f.user),
        ("Entry contains", f.entry_id), ("Smallest amount", f.min_amount),
        ("Largest amount", f.max_amount)) if v not in (None, "")]
    if f.side != "any":
        out.append(("Side", "Debits only" if f.side == "debit" else "Credits only"))
    if f.weekend:
        out.append(("Dated", "Weekends only"))
    return out


@router.post("/uploads/{upload_id}/explore")
def explore_upload(upload_id: int, body: ExploreIn, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    hits, result = explore(upload, body)
    log(session, user, "analysis.explore", f"Explored {upload.filename}", upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256,
                 "filters": dict(filters_text(body)), "matched": result["matched"]})
    session.commit()
    return trimmed(hits, result)


@router.get("/uploads/{upload_id}/explore/export")
def export_explore(upload_id: int, f: ExploreIn = Depends(), user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    hits, result = explore(upload, f)
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the ledger explorer workpaper for {upload.filename}", e,
        details={"upload_id": upload.id, "filters": dict(filters_text(f)),
                 "matched": result["matched"]})
    session.commit()
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()),
             ("File", f"{upload.filename} (SHA-256 {upload.sha256})"),
             *filters_text(f), ("Lines in the file", result["lines_in_file"]),
             ("Lines matched", result["matched"]), ("Debits", result["debits"]),
             ("Credits", result["credits"]), ("Net", result["net"])]
    if result["matched"] > MAX_EXPORT:
        about.append(("Note", f"Only the first {MAX_EXPORT:,} lines are listed."))
    lines = pd.DataFrame([line_out(r) for _, r in hits.head(MAX_EXPORT).iterrows()],
                         columns=["row", "date", "account", "entry_id", "description", "user",
                                  "amount"])
    lines.columns = ["Row", "Date", "Account", "Entry", "Description", "User", "Amount"]
    p = result["pivot"]
    first = GROUPS[p["rows_by"]]
    pv = pd.DataFrame([[r["key"], r["lines"], r["debits"], r["credits"], *r["cells"], r["net"]]
                       for r in p["rows"]],
                      columns=[first, "Lines", "Debits", "Credits", *p["columns"], "Net"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pv.to_excel(xl, sheet_name="Pivot", index=False)
        lines.to_excel(xl, sheet_name="Lines", index=False)
        finish_workbook(xl.book)
    stem = safe_stem(f"{e.client.name}-{e.name}-ledger-explorer")
    return Response(buf.getvalue(), media_type=XLSX, headers=attachment(f"{stem}.xlsx"))
