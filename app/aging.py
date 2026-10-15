"""Aging of a receivables or payables listing at the period end.

Each open item is aged by how many days it is past its due date. When the listing has no due
date column, the due date is the invoice date plus the credit terms the auditor enters. The
result shows the balance in each age band, the totals for each customer or vendor, the items
that need a closer look (long overdue, credit balances, dated after the period end), and how
the listing total compares with the trial balance line it supports.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd

from .exports import finish_workbook

SIDES = {"ar": "Receivables", "ap": "Payables"}
PARTY = {"ar": "Customer", "ap": "Vendor"}
BUCKETS = [
    ("current", "Not yet due", None, 0),
    ("d1_30", "1 to 30 days overdue", 1, 30),
    ("d31_60", "31 to 60 days overdue", 31, 60),
    ("d61_90", "61 to 90 days overdue", 61, 90),
    ("d91_180", "91 to 180 days overdue", 91, 180),
    ("over_180", "Over 180 days overdue", 181, None),
    ("no_date", "No date", None, None),
]
BUCKET_LABEL = {b[0]: b[1] for b in BUCKETS}
LONG_OVERDUE = 90  # days past due that count as long overdue
MAX_TERMS = 365
MAX_ROWS = 500  # parties and exceptions sent to the page; the Excel download has them all


class AgingError(ValueError):
    pass


def bucket_of(days: float | None) -> str:
    if days is None or pd.isna(days):
        return "no_date"
    for key, _, low, high in BUCKETS[:-1]:
        if (low is None or days >= low) and (high is None or days <= high):
            return key
    return "current"  # not reached: the bands cover every number of days


def _text(value) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def age(df: pd.DataFrame, side: str, as_of: date, terms_days: int = 30) -> dict:
    if side not in SIDES:
        raise AgingError("Pick a receivables or payables listing.")
    if not 0 <= terms_days <= MAX_TERMS:
        raise AgingError(f"Credit terms must be between 0 and {MAX_TERMS} days.")
    if df.empty:
        raise AgingError("The listing has no items to age.")
    amount = df["amount"].astype(float)
    # Payables are often exported as credits (negative). Show the listing as a positive
    # balance either way, so a credit balance on one item still stands out.
    flipped = bool(amount.sum() < 0)
    balance = -amount if flipped else amount
    as_of_ts = pd.Timestamp(as_of)
    invoice_date = (pd.to_datetime(df["date"], errors="coerce") if "date" in df
                    else pd.Series(pd.NaT, index=df.index))
    has_due = "due" in df and df["due"].notna().any()
    due = pd.to_datetime(df["due"], errors="coerce") if "due" in df else pd.Series(
        pd.NaT, index=df.index)
    # A missing due date on a line falls back to its invoice date plus the terms.
    due = due.fillna(invoice_date + pd.Timedelta(days=terms_days))
    days = (as_of_ts - due).dt.days
    party = (df["account"].map(_text) if "account" in df
             else pd.Series("", index=df.index)).replace("", "(none)")
    name = df["description"].map(_text) if "description" in df else pd.Series(
        "", index=df.index)
    invoice = df["entry_id"].map(_text) if "entry_id" in df else pd.Series("", index=df.index)
    lines = pd.DataFrame({
        "row": df["row"].to_numpy() if "row" in df else range(1, len(df) + 1),
        "party": party.to_numpy(), "name": name.to_numpy(), "invoice": invoice.to_numpy(),
        "date": invoice_date.to_numpy(), "due": due.to_numpy(),
        "days": days.to_numpy(), "balance": balance.round(2).to_numpy(),
    })
    lines["bucket"] = [bucket_of(None if pd.isna(d) else d) for d in lines["days"]]

    total = float(lines["balance"].sum())
    buckets = []
    for key, label, _, _ in BUCKETS:
        mask = lines["bucket"] == key
        value = float(lines.loc[mask, "balance"].sum())
        buckets.append({"id": key, "label": label, "items": int(mask.sum()),
                        "balance": round(value, 2),
                        "pct": round(100 * value / total, 1) if total else 0.0})

    parties = []
    for key, grp in lines.groupby("party", sort=False):
        names = grp["name"][grp["name"] != ""]
        row = {"party": key, "name": names.mode().iloc[0] if len(names) else "",
               "items": len(grp), "balance": round(float(grp["balance"].sum()), 2),
               "oldest_days": None if grp["days"].isna().all() else int(grp["days"].max()),
               "buckets": {b[0]: round(float(grp.loc[grp["bucket"] == b[0], "balance"].sum()), 2)
                           for b in BUCKETS}}
        row["long_overdue"] = round(sum(row["buckets"][k] for k in ("d91_180", "over_180")), 2)
        parties.append(row)
    parties.sort(key=lambda p: -abs(p["balance"]))

    long_mask = lines["days"] > LONG_OVERDUE
    credit_mask = lines["balance"] < 0
    after_mask = lines["date"] > as_of_ts
    exceptions = []
    for _, r in lines[long_mask | credit_mask | after_mask].iterrows():
        why = []
        if r["days"] > LONG_OVERDUE:
            why.append("Long overdue")
        if r["balance"] < 0:
            why.append("Credit balance")
        if pd.notna(r["date"]) and r["date"] > as_of_ts:
            why.append("Dated after the period end")
        exceptions.append({**line_out(r), "why": why})
    exceptions.sort(key=lambda x: -abs(x["balance"]))

    def amount_of(mask) -> float:
        return round(float(lines.loc[mask, "balance"].sum()), 2)

    return {
        "side": side, "side_label": SIDES[side], "party_label": PARTY[side],
        "as_of": as_of.isoformat(), "terms_days": terms_days,
        "basis": "due" if has_due else "terms", "flipped": flipped,
        "totals": {
            "items": len(lines), "parties": len(parties), "balance": round(total, 2),
            "overdue": amount_of(lines["days"] > 0),
            "long_overdue": amount_of(long_mask), "long_overdue_items": int(long_mask.sum()),
            "credit_balances": amount_of(credit_mask), "credit_items": int(credit_mask.sum()),
            "after_period_end": amount_of(after_mask), "after_items": int(after_mask.sum()),
            "no_date": amount_of(lines["bucket"] == "no_date"),
        },
        "buckets": buckets, "parties": parties, "exceptions": exceptions,
        "lines": [line_out(r) for _, r in lines.iterrows()],
    }


def _day(value) -> str | None:
    return None if pd.isna(value) else pd.Timestamp(value).date().isoformat()


def line_out(r) -> dict:
    return {"row": int(r["row"]), "party": r["party"], "name": r["name"],
            "invoice": r["invoice"], "date": _day(r["date"]), "due": _day(r["due"]),
            "days": None if pd.isna(r["days"]) else int(r["days"]),
            "bucket": r["bucket"], "balance": float(r["balance"])}


def with_ledger(result: dict, ledger: dict | None) -> dict:
    """Add the trial balance line (adjusted lead sheet total) the listing should agree to."""
    if ledger is not None:
        diff = round(result["totals"]["balance"] - ledger["balance"], 2)
        ledger = {**ledger, "difference": diff}
    return {**result, "ledger": ledger}


def trimmed(result: dict) -> dict:
    """The result for the page: no line list, and parties and exceptions cut to MAX_ROWS."""
    out = {k: v for k, v in result.items() if k != "lines"}
    out["parties"] = result["parties"][:MAX_ROWS]
    out["exceptions"] = result["exceptions"][:MAX_ROWS]
    out["parties_truncated"] = max(0, len(result["parties"]) - MAX_ROWS)
    out["exceptions_truncated"] = max(0, len(result["exceptions"]) - MAX_ROWS)
    return out


def to_excel(result: dict, context: dict | None = None) -> bytes:
    t = result["totals"]
    party = result["party_label"]
    basis = ("Due dates in the listing" if result["basis"] == "due"
             else f"Invoice date plus {result['terms_days']} days' credit terms")
    about = list((context or {}).items()) + [
        ("Aged at", result["as_of"]), ("Aged from", basis),
        ("Signs", "Flipped so the listing total is positive" if result["flipped"]
         else "As in the file"),
        ("Items", t["items"]), (f"{party}s", t["parties"]), ("Listing total", t["balance"]),
        ("Overdue", t["overdue"]), (f"Over {LONG_OVERDUE} days overdue", t["long_overdue"]),
        ("Credit balances", t["credit_balances"]),
        ("Dated after the period end", t["after_period_end"]),
    ]
    if result.get("ledger"):
        lg = result["ledger"]
        about += [(f"Trial balance: {lg['label']}", lg["balance"]),
                  ("Difference (listing less trial balance)", lg["difference"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame([{"Age": b["label"], "Items": b["items"], "Balance": b["balance"],
                       "% of total": b["pct"]} for b in result["buckets"]]).to_excel(
            xl, sheet_name="Aging", index=False)
        pd.DataFrame([{party: p["party"], "Name": p["name"], "Items": p["items"],
                       "Balance": p["balance"],
                       **{BUCKET_LABEL[k]: v for k, v in p["buckets"].items()},
                       "Oldest (days overdue)": p["oldest_days"]}
                      for p in result["parties"]]).to_excel(
            xl, sheet_name=f"By {party.lower()}", index=False)
        columns = ["Row", party, "Name", "Invoice", "Date", "Due", "Days overdue", "Age",
                   "Balance"]

        def rows(items, why=False):
            return pd.DataFrame([{
                "Row": x["row"], party: x["party"], "Name": x["name"], "Invoice": x["invoice"],
                "Date": x["date"], "Due": x["due"], "Days overdue": x["days"],
                "Age": BUCKET_LABEL[x["bucket"]], "Balance": x["balance"],
                **({"Why": ", ".join(x["why"])} if why else {})} for x in items],
                columns=columns + (["Why"] if why else []))
        rows(result["exceptions"], why=True).to_excel(xl, sheet_name="Exceptions", index=False)
        rows(result["lines"]).to_excel(xl, sheet_name="All items", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()
