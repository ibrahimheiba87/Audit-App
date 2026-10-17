"""Account pairs: the general ledger as flows between statement lines, the way Inflo and
MindBridge show it. In each journal entry, every debit is paired with the entry's credits
(shared out in proportion when there are several), so the whole ledger reads as "this much
went from payables to cash, this much from revenue to receivables".

Then the pairs worth a look (ISA 240): revenue credited against something other than
receivables, cash or deferred income, revenue reversed the same way, expenses moved onto the
balance sheet as assets, equity moved against profit or loss, cash paid out against equity,
and pairs that hardly ever happen in this ledger. Nothing is saved; each look is logged, and it
downloads to Excel. It only reads, so it works on locked files too.
"""

from __future__ import annotations

import io
from collections import defaultdict

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .auth import current_user, log
from .db import Engagement, Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem
from .exports import attachment, finish_workbook
from .posters import lines_of, money, text
from .statements import LINE, SECTION

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_SHOWN = 200        # entries per list on the page; the download has them all
MAX_EXPORT = 20_000
MAX_ACCOUNT_PAIRS = 500
RARE = 2               # a pair of lines used in this many entries or fewer is rare ...
MIN_ENTRIES = 100      # ... in a ledger with at least this many entries
UNKNOWN = "unknown"    # lines with no statement line

REVENUE_PARTNERS = {"receivables", "cash", "accruals", "other_current_assets", "revenue"}
EQUITY = {"share_capital", "retained_earnings"}
ASSETS = {"ppe", "intangibles", "inventory", "other_noncurrent_assets"}
CHECKS = {
    "revenue_credit": "Revenue credited against an unusual account",
    "revenue_debit": "Revenue reversed against an unusual account",
    "capitalised": "Expenses moved to assets",
    "equity_pl": "Equity moved against profit or loss",
    "cash_equity": "Cash paid out against equity",
    "rare": "Rarely used pairs",
}
SHEETS = {"revenue_credit": "Revenue credited", "revenue_debit": "Revenue reversed",
          "capitalised": "Expenses to assets", "equity_pl": "Equity and P&L",
          "cash_equity": "Cash and equity", "rare": "Rare pairs"}


def label(line: str) -> str:
    return LINE[line]["label"] if line in LINE else "No statement line"


def is_pl(line: str) -> bool:
    return line in LINE and SECTION[LINE[line]["section"]].statement == "is"


def flags_of(dr: str, cr: str) -> list[str]:
    """The checks a flow from the credited line `cr` to the debited line `dr` falls under."""
    out = []
    if cr == "revenue" and dr not in REVENUE_PARTNERS and dr != UNKNOWN:
        out.append("revenue_credit")
    if dr == "revenue" and cr not in REVENUE_PARTNERS and cr != UNKNOWN:
        out.append("revenue_debit")
    if dr in ASSETS and is_pl(cr) and LINE[cr]["section"] in (
            "cost_of_sales", "operating_expenses", "finance_costs"):
        out.append("capitalised")
    if (dr in EQUITY and is_pl(cr)) or (cr in EQUITY and is_pl(dr)):
        out.append("equity_pl")
    if cr == "cash" and dr in EQUITY:
        out.append("cash_equity")
    return out


def analyse(df: pd.DataFrame, e: Engagement) -> dict:
    if "entry_id" not in df:
        raise HTTPException(400, "This ledger has no entry numbers, so its lines can't be put "
                                 "together into entries. Map the Entry column of the file, "
                                 "then look again.")
    df = df.assign(_entry=text(df, "entry_id"), _line=lines_of(df, e).fillna(UNKNOWN),
                   _account=text(df, "account"), _user=text(df, "user"),
                   _description=text(df, "description"))
    df = df[(df["_entry"] != "") & (df["amount"] != 0)]
    # Entries with debits but no credits, or the other way round, can't be paired.
    sides = df.groupby("_entry", sort=False)["amount"].agg(
        has_dr=lambda a: (a > 0).any(), has_cr=lambda a: (a < 0).any())
    both = sides.index[sides["has_dr"] & sides["has_cr"]]
    one_sided = len(sides) - len(both)
    df = df[df["_entry"].isin(both)]
    dr = df[df["amount"] > 0][["_entry", "_line", "_account", "amount"]]
    cr = df[df["amount"] < 0][["_entry", "_line", "_account", "amount"]]
    cr = cr.assign(share=cr["amount"] / cr.groupby("_entry")["amount"].transform("sum"))
    flows = dr.merge(cr[["_entry", "_line", "_account", "share"]], on="_entry",
                     suffixes=("_dr", "_cr"))
    flows["flow"] = flows["amount"] * flows["share"]

    def grouped(a: str, b: str) -> dict:
        g = flows.groupby([a, b], sort=False).agg(entries=("_entry", lambda x: set(x)),
                                                  amount=("flow", "sum"))
        return {k: {"entries": r.entries, "amount": r.amount}
                for k, r in zip(g.index, g.itertuples(), strict=True)}
    lines = grouped("_line_dr", "_line_cr")
    accounts = grouped("_account_dr", "_account_cr")
    entry_pairs: dict[str, set] = defaultdict(set)
    for key, d, c in zip(flows["_entry"], flows["_line_dr"], flows["_line_cr"], strict=True):
        entry_pairs[key].add((d, c))
    df = df.assign(_debit=df["amount"].clip(lower=0))
    info = df.groupby("_entry", sort=False).agg(
        row=("row", "min"), user=("_user", "first"), description=("_description", "first"),
        debits=("_debit", "sum"),
        accounts=("_account", lambda a: ", ".join(sorted({x for x in a if x})[:6])),
        **({"date": ("date", "min")} if "date" in df else {}))
    entry_info = {key: {
        "entry_id": key, "row": int(r["row"]),
        "date": r["date"].date().isoformat() if pd.notna(r.get("date")) else None,
        "user": r["user"], "description": r["description"], "accounts": r["accounts"],
        "debits": money(r["debits"])} for key, r in info.iterrows()}
    entries = len(entry_info)
    rare_applies = entries >= MIN_ENTRIES
    pair_rows = []
    for (d, c), v in lines.items():
        flags = flags_of(d, c)
        if rare_applies and len(v["entries"]) <= RARE:
            flags.append("rare")
        pair_rows.append({"debit": d, "debit_label": label(d), "credit": c,
                          "credit_label": label(c), "entries": len(v["entries"]),
                          "amount": money(v["amount"]), "flags": flags})
    pair_rows.sort(key=lambda r: -r["amount"])
    flagged_pairs = {(r["debit"], r["credit"]): r["flags"] for r in pair_rows if r["flags"]}
    checks = []
    for k, name in CHECKS.items():
        keys = [key for key, ps in entry_pairs.items()
                if any(k in flagged_pairs.get(p, ()) for p in ps)]
        items = sorted((entry_info[key] for key in keys), key=lambda i: -i["debits"])
        checks.append({"check": k, "label": name, "count": len(items),
                       "debits": money(sum(i["debits"] for i in items)), "items": items})
    account_rows = sorted(({"debit": d, "credit": c, "entries": len(v["entries"]),
                            "amount": money(v["amount"])} for (d, c), v in accounts.items()),
                          key=lambda r: -r["amount"])
    known = df["_line"] != UNKNOWN
    return {
        "lines": len(df), "entries": entries, "one_sided": one_sided,
        "total": money(sum(i["debits"] for i in entry_info.values())),
        "lines_known_pct": round(known.mean() * 100, 1) if len(df) else 0,
        "mapped": bool(e.account_mappings), "rare_applies": rare_applies, "rare_limit": RARE,
        "pairs": pair_rows, "accounts": account_rows[:MAX_ACCOUNT_PAIRS],
        "account_pairs": len(account_rows), "checks": checks}


def run(upload: Upload) -> dict:
    if upload.kind != "gl":
        raise HTTPException(400, "Pick a general ledger.")
    return analyse(load_upload(upload).df, upload.engagement)


@router.post("/uploads/{upload_id}/account-pairs")
def account_pairs(upload_id: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    result = run(upload)
    log(session, user, "analysis.account_pairs",
        f"Looked at the account pairs in {upload.filename}", upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256, "entries": result["entries"],
                 "pairs": len(result["pairs"]),
                 "flagged": {c["check"]: c["count"] for c in result["checks"]}})
    session.commit()
    return {"upload_id": upload.id, "filename": upload.filename, **result,
            "accounts": result["accounts"][:MAX_SHOWN],
            "checks": [{**c, "items": c["items"][:MAX_SHOWN]} for c in result["checks"]]}


@router.get("/uploads/{upload_id}/account-pairs/export")
def export_account_pairs(upload_id: int, user: User = Depends(current_user),
                         session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    r = run(upload)
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the account pairs workpaper for {upload.filename}", e,
        details={"upload_id": upload.id})
    session.commit()
    about = pd.DataFrame([
        ("Client", e.client.name), ("Engagement", e.name),
        ("Period end", e.period_end.isoformat()),
        ("File", f"{upload.filename} (SHA-256 {upload.sha256})"),
        ("Entries paired", r["entries"]), ("Entries with only debits or credits", r["one_sided"]),
        ("Value paired", r["total"]),
        ("Lines with a known statement line", f"{r['lines_known_pct']}%"),
        ("How", "Each debit is paired with the entry's credits, shared out in proportion."),
        *[(c["label"], c["count"]) for c in r["checks"]]], columns=["Item", "Value"])
    pairs = pd.DataFrame([{
        "Debited": p["debit_label"], "Credited": p["credit_label"], "Entries": p["entries"],
        "Amount": p["amount"], "Worth a look": ", ".join(CHECKS[f] for f in p["flags"])}
        for p in r["pairs"]])
    accounts = pd.DataFrame([{"Debited account": a["debit"], "Credited account": a["credit"],
                              "Entries": a["entries"], "Amount": a["amount"]}
                             for a in r["accounts"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        pairs.to_excel(xl, sheet_name="Statement lines", index=False)
        accounts.to_excel(xl, sheet_name="Accounts", index=False)
        for c in r["checks"]:
            if not c["count"]:
                continue
            pd.DataFrame([{"Entry": i["entry_id"], "Date": i["date"], "User": i["user"],
                           "Description": i["description"], "Accounts": i["accounts"],
                           "Debits": i["debits"]} for i in c["items"][:MAX_EXPORT]]).to_excel(
                xl, sheet_name=SHEETS[c["check"]], index=False)
        finish_workbook(xl.book)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers=attachment(f"{safe_stem(upload.filename)} - account pairs.xlsx"))
