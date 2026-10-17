"""The fixed asset register: totals by category, additions and disposals in the year, the
depreciation charge recomputed asset by asset, odd entries, and a tie-out to the trial balance.

The client's register is uploaded as a list. The auditor says which column holds the asset
number, cost, accumulated depreciation, this year's charge and so on (guessed from the
headers until then). Depreciation is recomputed straight line, from the useful life or the
annual rate, for the part of the year each asset was held, and never below a nil book value.
The register's net book value and charge are compared with the property, plant and equipment
and depreciation lines of the lead sheets. Nothing is saved; each look is logged, and the
result downloads to Excel. It only reads, so it works on locked files too.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import listings
from .auth import current_user, log
from .db import Engagement, Upload, User, get_session
from .engagements import get_or_404, safe_stem
from .exports import attachment, finish_workbook
from .leadsheets import compute

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KINDS = ("fixed_assets",)
FIELDS = {
    "asset": "Asset number", "description": "Description", "category": "Category",
    "acquired": "Date acquired", "disposed": "Date disposed", "cost": "Cost",
    "accumulated": "Accumulated depreciation", "charge": "Depreciation this year",
    "life": "Useful life (years)", "rate": "Depreciation rate %",
}
SYNONYMS = {
    "asset": ["assetno", "assetnumber", "assetid", "assetcode", "asset", "tagno", "tagnumber",
              "tag", "code"],
    "description": ["description", "assetdescription", "assetname", "name", "details", "item"],
    "category": ["category", "assetcategory", "assetclass", "class", "type", "assettype",
                 "group"],
    "acquired": ["acquisitiondate", "dateacquired", "acquired", "purchasedate",
                 "dateofpurchase", "inservicedate", "capitalisationdate",
                 "capitalizationdate", "date"],
    "disposed": ["disposaldate", "datedisposed", "disposed", "solddate", "retirementdate",
                 "retireddate"],
    "cost": ["cost", "originalcost", "acquisitioncost", "grosscost", "closingcost",
             "historicalcost", "purchasecost", "costclosing", "grossbookvalue"],
    "accumulated": ["accumulateddepreciation", "accdepreciation", "accdep", "accumdep",
                    "accumulateddep", "depreciationtodate", "totaldepreciation",
                    "closingaccumulateddepreciation"],
    "charge": ["depreciationcharge", "depreciationfortheyear", "depreciationforyear",
               "yeardepreciation", "currentdepreciation", "depreciationexpense",
               "ytddepreciation", "annualdepreciation", "charge", "depreciation"],
    "life": ["usefullife", "usefullifeyears", "lifeyears", "life", "years"],
    "rate": ["depreciationrate", "rate", "annualrate", "depreciationratepercent"],
}
MAX_SHOWN = 200
Column = Annotated[str, StringConstraints(max_length=255)]


class FixedAssetsIn(BaseModel):
    upload_id: int | None = None  # for the download, which is a GET
    columns: dict[str, Column] = Field(default_factory=dict, max_length=len(FIELDS))


class FixedAssetError(ValueError):
    head: dict = {}


def money(v) -> float:
    return round(float(v), 2) + 0.0


def period(e: Engagement) -> tuple[date, date]:
    start = e.period_start or (e.period_end.replace(year=e.period_end.year - 1)
                               if not (e.period_end.month == 2 and e.period_end.day == 29)
                               else date(e.period_end.year - 1, 2, 28)) + timedelta(days=1)
    return start, e.period_end


def analyse(raw: pd.DataFrame, cols: dict[str, str], e: Engagement,
            tb_lines: dict[str, float] | None) -> dict:
    """Everything the page and the workbook show."""
    if "cost" not in cols:
        raise FixedAssetError("Say which column holds each asset's cost.")
    start, end = period(e)
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    acquired, disposed = listings.dates(raw, [cols.get("acquired"), cols.get("disposed")])
    cost = listings.numbers(raw, cols.get("cost")).fillna(0.0)
    acc = listings.numbers(raw, cols.get("accumulated")).fillna(0.0).abs()  # often negative
    charge = listings.numbers(raw, cols.get("charge"))
    life = listings.numbers(raw, cols.get("life"))
    rate = listings.numbers(raw, cols.get("rate"))
    df = pd.DataFrame({
        "row": raw["__row"], "asset": listings.text(raw, cols.get("asset")),
        "description": listings.text(raw, cols.get("description")),
        "category": listings.text(raw, cols.get("category")).replace("", "(none)"),
        "acquired": acquired, "disposed": disposed, "cost": cost, "accumulated": acc,
        "charge": charge.fillna(0.0), "life": life, "rate": rate,
    })
    df["nbv"] = df["cost"] - df["accumulated"]
    gone = df["disposed"].notna() & (df["disposed"] <= last)
    held = df[~gone]

    # Straight line, for the part of the year each asset was held.
    days = (last - first).days + 1
    begin = df["acquired"].where(df["acquired"] > first, first).fillna(first)
    finish = df["disposed"].where(df["disposed"] < last, last).fillna(last)
    held_days = ((finish - begin).dt.days + 1).clip(lower=0)
    annual = (df["cost"] * df["rate"] / 100).where(df["rate"].notna() & (df["rate"] > 0),
                                                   df["cost"] / df["life"].where(df["life"] > 0))
    opening_nbv = df["cost"] - (df["accumulated"] - df["charge"])
    expected = (annual * held_days / days).clip(upper=opening_nbv.clip(lower=0))
    can = annual.notna() & ("charge" in cols)
    df["expected"] = expected.where(can).round(2)
    df["difference"] = (df["charge"] - df["expected"]).where(can).round(2)
    off = can & (df["difference"].abs() > (df["expected"].abs() * 0.01).clip(lower=1))

    issues: list[tuple[int, str]] = []

    def flag(mask: pd.Series, text: str) -> None:
        issues.extend((int(i), text) for i in df.index[mask.fillna(False)])

    flag(df["accumulated"] > df["cost"] + 0.005, "Accumulated depreciation is more than cost")
    flag(df["cost"] <= 0, "Cost is nil or negative")
    flag(df["acquired"] > last, "Acquired after the period end")
    if "acquired" in cols:
        flag(df["acquired"].isna(), "No date acquired")
    flag(gone & (df["nbv"].abs() > 0.005), "Disposed but still carries a book value")
    if "charge" in cols:
        flag((opening_nbv <= 0.005) & (df["charge"] > 0.005),
             "Charged again after being fully depreciated")
    if "asset" in cols:
        ids = df["asset"].where(df["asset"] != "")
        flag(ids.duplicated(keep=False) & ids.notna(), "The same asset number more than once")
    by_row: dict[int, list[str]] = {}
    for i, text in issues:
        by_row.setdefault(i, []).append(text)

    def rows(frame: pd.DataFrame, extra=()) -> list[dict]:
        out = []
        for i, r in frame.iterrows():
            out.append({
                "row": int(r["row"]), "asset": r["asset"], "description": r["description"],
                "category": r["category"],
                "acquired": r["acquired"].date().isoformat() if pd.notna(r["acquired"]) else None,
                "disposed": r["disposed"].date().isoformat() if pd.notna(r["disposed"]) else None,
                "cost": money(r["cost"]), "accumulated": money(r["accumulated"]),
                "nbv": money(r["nbv"]), "charge": money(r["charge"]),
                **{k: (None if pd.isna(r[k]) else money(r[k])) for k in extra},
                "issues": by_row.get(i, [])})
        return out

    categories = []
    for name, g in held.groupby("category", sort=True):
        categories.append({"category": name, "assets": len(g), "cost": money(g["cost"].sum()),
                           "accumulated": money(g["accumulated"].sum()),
                           "nbv": money(g["nbv"].sum()), "charge": money(g["charge"].sum())})
    totals = {"assets": len(held), "cost": money(held["cost"].sum()),
              "accumulated": money(held["accumulated"].sum()),
              "nbv": money(held["nbv"].sum()), "charge": money(df["charge"].sum())}
    additions = df[(df["acquired"] >= first) & (df["acquired"] <= last)]
    disposals = df[(df["disposed"] >= first) & (df["disposed"] <= last)]
    recomputed = df[off].reindex(df[off]["difference"].abs().sort_values(ascending=False).index)
    flagged = df.loc[sorted(by_row)]

    tie = []
    if tb_lines is not None:
        for label, line, ours in (("Property, plant and equipment (net book value)", "ppe",
                                   totals["nbv"]),
                                  ("Depreciation and amortisation", "depreciation",
                                   totals["charge"])):
            if line == "depreciation" and "charge" not in cols:
                continue
            books = tb_lines.get(line)
            tie.append({"line": line, "label": label, "register": ours,
                        "trial_balance": books,
                        "difference": None if books is None else money(ours - books)})
    return {
        "columns": cols, "period_start": start.isoformat(), "period_end": end.isoformat(),
        "rows": len(df), "totals": totals, "categories": categories,
        "additions": {"count": len(additions), "cost": money(additions["cost"].sum()),
                      "items": rows(additions.sort_values("cost", ascending=False))},
        "disposals": {"count": len(disposals), "cost": money(disposals["cost"].sum()),
                      "nbv": money(disposals["nbv"].sum()),
                      "items": rows(disposals.sort_values("cost", ascending=False))},
        "recomputed": {"checked": int(can.sum()),
                       "differences": int(off.sum()),
                       "expected": money(df["expected"].sum()),
                       "charged": money(df.loc[can, "charge"].sum()),
                       "net_difference": money(df.loc[can, "difference"].sum()),
                       "items": rows(recomputed, ("expected", "difference"))},
        "issues": {"count": len(by_row), "items": rows(flagged)},
        "tie": tie,
    }


def trimmed(result: dict) -> dict:
    out = dict(result)
    for k in ("additions", "disposals", "recomputed", "issues"):
        out[k] = {**result[k], "items": result[k]["items"][:MAX_SHOWN]}
    return out


def tb_figures(session: Session, e: Engagement) -> dict[str, float] | None:
    try:
        data, tb, _ = compute(session, e, None, None)
    except HTTPException:
        return None
    if tb is None:
        return None
    return {s["line"]: s["totals"]["adjusted"] for s in data["lead_sheets"]}


def run(session: Session, upload: Upload, columns: dict[str, str]) -> tuple[dict, dict]:
    """The file's columns and fields, and the analysis. A column still to pick is raised as
    a FixedAssetError carrying the first part, so the page can show the pickers."""
    raw = listings.read(upload, KINDS, "a fixed asset register")
    cols = listings.columns_for(raw, columns, SYNONYMS)
    e = upload.engagement
    head = {"upload_id": upload.id, "filename": upload.filename,
            "file_columns": [c for c in raw.columns if c != "__row"],
            "fields": [{"id": k, "label": v} for k, v in FIELDS.items()], "columns": cols}
    try:
        return head, analyse(raw, cols, e, tb_figures(session, e))
    except FixedAssetError as exc:
        exc.head = head
        raise


@router.post("/uploads/{upload_id}/fixed-assets")
def fixed_assets(upload_id: int, body: FixedAssetsIn, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    try:
        head, result = run(session, upload, body.columns)
    except FixedAssetError as exc:
        return {**exc.head, "problem": str(exc)}
    log(session, user, "analysis.fixed_assets", f"Analysed the fixed asset register "
        f"{upload.filename}", upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256, "columns": result["columns"],
                 "assets": result["rows"], "issues": result["issues"]["count"],
                 "recomputed_differences": result["recomputed"]["differences"]})
    session.commit()
    return {**head, **trimmed(result)}


def to_excel(e: Engagement, upload: Upload, r: dict) -> bytes:
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period", f"{r['period_start']} to {r['period_end']}"),
             ("File", f"{upload.filename} (SHA-256 {upload.sha256})"),
             *((f"Column for {FIELDS[k].lower()}", v) for k, v in r["columns"].items()),
             ("Assets in the register", r["rows"]), ("Assets held at the period end",
                                                     r["totals"]["assets"]),
             ("Cost", r["totals"]["cost"]), ("Accumulated depreciation",
                                             r["totals"]["accumulated"]),
             ("Net book value", r["totals"]["nbv"]), ("Depreciation this year",
                                                      r["totals"]["charge"]),
             ("Depreciation recomputed on", f"{r['recomputed']['checked']} assets"),
             ("Recomputed charge", r["recomputed"]["expected"]),
             ("Net difference (charged less recomputed)", r["recomputed"]["net_difference"])]
    for t in r["tie"]:
        about.append((f"{t['label']}: register less trial balance", t["difference"]))

    def sheet(items: list[dict], extra: tuple[str, ...] = ()) -> pd.DataFrame:
        cols = ["row", "asset", "description", "category", "acquired", "disposed", "cost",
                "accumulated", "nbv", "charge", *extra, "issues"]
        frame = pd.DataFrame([{**x, "issues": "; ".join(x["issues"])} for x in items],
                             columns=cols)
        frame.columns = ["Row", "Asset", "Description", "Category", "Acquired", "Disposed",
                         "Cost", "Accumulated depreciation", "Net book value",
                         "Depreciation this year",
                         *(x.replace("expected", "Recomputed").replace("difference",
                                                                       "Difference")
                           for x in extra), "Points to look at"]
        return frame

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame([{"Category": c["category"], "Assets": c["assets"], "Cost": c["cost"],
                       "Accumulated depreciation": c["accumulated"],
                       "Net book value": c["nbv"], "Depreciation this year": c["charge"]}
                      for c in r["categories"]],
                     columns=["Category", "Assets", "Cost", "Accumulated depreciation",
                              "Net book value", "Depreciation this year"]).to_excel(
            xl, sheet_name="By category", index=False)
        pd.DataFrame([{"Line": t["label"], "Register": t["register"],
                       "Trial balance": t["trial_balance"], "Difference": t["difference"]}
                      for t in r["tie"]],
                     columns=["Line", "Register", "Trial balance", "Difference"]).to_excel(
            xl, sheet_name="Tie to the TB", index=False)
        sheet(r["additions"]["items"]).to_excel(xl, sheet_name="Additions", index=False)
        sheet(r["disposals"]["items"]).to_excel(xl, sheet_name="Disposals", index=False)
        sheet(r["recomputed"]["items"], ("expected", "difference")).to_excel(
            xl, sheet_name="Depreciation differences", index=False)
        sheet(r["issues"]["items"]).to_excel(xl, sheet_name="Points to look at", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.post("/uploads/{upload_id}/fixed-assets/export")
def export_fixed_assets(upload_id: int, body: FixedAssetsIn,
                        user: User = Depends(current_user),
                        session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    try:
        _, result = run(session, upload, body.columns)
    except FixedAssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the fixed asset workpaper for {upload.filename}", e,
        details={"upload_id": upload.id, "columns": result["columns"]})
    session.commit()
    stem = safe_stem(f"{e.client.name}-{e.name}-fixed-assets")
    return Response(to_excel(e, upload, result), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))
