"""Who posted what: the general ledger looked at by the people who posted it (ISA 240 journal
entry testing). Per user, how many entries and how much they posted, which statement lines
they touched and when; then the entries worth a look: the ones a person approved themselves,
ones with no approver or no user, ones that move both cash and revenue, and the entries of
people who post only now and then. Nothing is saved; each look is logged, and it downloads
to Excel. It only reads, so it works on locked files too.
"""

from __future__ import annotations

import io

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .auth import current_user, log
from .db import Engagement, Upload, User, get_session
from .engagements import get_or_404, load_upload, safe_stem
from .exports import attachment, finish_workbook
from .statements import LINE, suggest_line

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_SHOWN = 200        # entries per list on the page; the download has them all
MAX_EXPORT = 20_000
OCCASIONAL = 3         # a user with this many entries or fewer posts only now and then
MIN_ENTRIES = 30       # ... in a ledger with at least this many entries
CHECKS = {
    "self_approved": "Approved by the person who posted it",
    "no_approver": "No approver recorded",
    "no_user": "No user recorded",
    "cash_revenue": "Moves both cash and revenue",
    "occasional": "Posted by someone who posts only now and then",
}
SHEETS = {"self_approved": "Own approval", "no_approver": "No approver", "no_user": "No user",
          "cash_revenue": "Cash and revenue", "occasional": "Occasional posters"}


def money(v) -> float:
    return round(float(v), 2) + 0.0


def text(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df:
        return pd.Series([""] * len(df), index=df.index, dtype="object")
    return df[col].astype("object").where(df[col].notna(), "").astype(str).str.strip()


def lines_of(df: pd.DataFrame, e: Engagement) -> pd.Series:
    """Each line's statement line: the engagement's account mapping, else a guess from the
    account's name or number."""
    mapping = {m.account: m.line for m in e.account_mappings}
    guesses: dict[str, str | None] = {}

    def line(account: str) -> str | None:
        if account in mapping:
            return mapping[account]
        if account not in guesses:
            guesses[account] = suggest_line(account, None) if account else None
        return guesses[account]
    return text(df, "account").map(line)


def analyse(df: pd.DataFrame, e: Engagement) -> dict:
    if "user" not in df:
        raise HTTPException(400, "This ledger has no column saying who posted each line. "
                                 "Map the User column of the file, then look again.")
    df = df.assign(_user=text(df, "user"), _approver=text(df, "approver"),
                   _line=lines_of(df, e))
    has_entries = "entry_id" in df
    # Lines without an entry number are each their own entry.
    df["_entry"] = text(df, "entry_id") if has_entries else df["row"].astype(str)
    df.loc[df["_entry"] == "", "_entry"] = "row " + df["row"].astype(str)
    weekend = df["date"].dt.dayofweek >= 5 if "date" in df else pd.Series(False, index=df.index)
    df["_weekend"] = weekend.fillna(False)

    entries = df.groupby("_entry", sort=False).agg(
        row=("row", "min"), user=("_user", "first"), approver=("_approver", "first"),
        debits=("amount", lambda a: a[a > 0].sum()), lines=("row", "size"),
        accounts=("account", lambda a: ", ".join(sorted({str(x) for x in a.dropna()})[:6]))
        if "account" in df else ("row", lambda a: ""),
        weekend=("_weekend", "any"),
        cash=("_line", lambda x: (x == "cash").any()),
        revenue=("_line", lambda x: (x == "revenue").any()),
        **({"date": ("date", "min")} if "date" in df else {}),
        **({"description": ("description", "first")} if "description" in df else {}))
    users = entries.groupby("user", sort=False)
    total = len(entries)

    by_user = []
    for name, g in users:
        posted = df[df["_user"] == name]
        top = posted["_line"].dropna().value_counts().head(3)
        by_user.append({
            "user": name or "(blank)", "entries": len(g), "lines": int(g["lines"].sum()),
            "share_pct": round(len(g) / total * 100, 1) if total else 0,
            "debits": money(g["debits"].sum()), "largest": money(g["debits"].max()),
            "first": g["date"].min().date().isoformat() if "date" in g and g["date"].notna().any()
            else None,
            "last": g["date"].max().date().isoformat() if "date" in g and g["date"].notna().any()
            else None,
            "weekend": int(g["weekend"].sum()),
            "self_approved": int(((g["approver"] != "") & (g["approver"].str.lower() ==
                                                          g["user"].str.lower())).sum()),
            "cash_revenue": int((g["cash"] & g["revenue"]).sum()),
            "lines_touched": [LINE[k]["label"] for k in top.index if k in LINE]})
    by_user.sort(key=lambda r: -r["debits"])

    has_approver = "approver" in df
    occasional_users = ({r["user"] for r in by_user if r["entries"] <= OCCASIONAL
                         and r["user"] != "(blank)"} if total >= MIN_ENTRIES else set())
    masks = {
        "self_approved": (entries["approver"] != "")
        & (entries["approver"].str.lower() == entries["user"].str.lower()),
        "no_approver": entries["approver"] == "" if has_approver
        else pd.Series(False, index=entries.index),
        "no_user": entries["user"] == "",
        "cash_revenue": entries["cash"] & entries["revenue"],
        "occasional": entries["user"].isin(occasional_users),
    }

    def item(key, r) -> dict:
        day = r.get("date")
        return {"entry_id": key if has_entries and not key.startswith("row ") else "",
                "row": int(r["row"]), "user": r["user"], "approver": r["approver"],
                "date": day.date().isoformat() if isinstance(day, pd.Timestamp) and pd.notna(day)
                else None,
                "description": "" if pd.isna(r.get("description")) else str(r.get("description", "")),
                "accounts": r["accounts"], "debits": money(r["debits"]), "lines": int(r["lines"])}

    checks = []
    for k, label in CHECKS.items():
        hit = entries[masks[k]].sort_values("debits", ascending=False)
        checks.append({"check": k, "label": label, "count": len(hit),
                       "debits": money(hit["debits"].sum()),
                       "items": [item(key, r) for key, r in hit.iterrows()]})
    known = df["_line"].notna()
    return {
        "lines": len(df), "entries": total, "users": len(by_user),
        "has_entries": has_entries, "has_approver": has_approver, "has_dates": "date" in df,
        "lines_known_pct": round(known.mean() * 100, 1) if len(df) else 0,
        "mapped": bool(e.account_mappings), "occasional_limit": OCCASIONAL,
        "occasional_applies": total >= MIN_ENTRIES,
        "by_user": by_user, "checks": checks}


def run(upload: Upload) -> dict:
    if upload.kind != "gl":
        raise HTTPException(400, "Pick a general ledger.")
    return analyse(load_upload(upload).df, upload.engagement)


def trimmed(result: dict) -> dict:
    return {**result, "checks": [{**c, "items": c["items"][:MAX_SHOWN]}
                                 for c in result["checks"]]}


@router.post("/uploads/{upload_id}/posters")
def posters(upload_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    result = run(upload)
    log(session, user, "analysis.posters", f"Looked at who posted {upload.filename}",
        upload.engagement, details={"upload_id": upload.id, "sha256": upload.sha256,
                                    "users": result["users"],
                                    "flagged": {c["check"]: c["count"] for c in result["checks"]}})
    session.commit()
    return {"upload_id": upload.id, "filename": upload.filename, **trimmed(result)}


@router.get("/uploads/{upload_id}/posters/export")
def export_posters(upload_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    r = run(upload)
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the who posted what workpaper for {upload.filename}", e,
        details={"upload_id": upload.id})
    session.commit()
    about = pd.DataFrame([
        ("Client", e.client.name), ("Engagement", e.name),
        ("Period end", e.period_end.isoformat()),
        ("File", f"{upload.filename} (SHA-256 {upload.sha256})"),
        ("Lines", r["lines"]), ("Entries", r["entries"]), ("Users", r["users"]),
        ("Lines with a known statement line", f"{r['lines_known_pct']}%"),
        ("Approver column", "Yes" if r["has_approver"] else "Not in the file"),
        *[(c["label"], c["count"]) for c in r["checks"]]], columns=["Item", "Value"])
    people = pd.DataFrame([{
        "User": u["user"], "Entries": u["entries"], "Lines": u["lines"],
        "Share of entries %": u["share_pct"], "Debits": u["debits"],
        "Largest entry": u["largest"], "First": u["first"], "Last": u["last"],
        "Weekend entries": u["weekend"], "Approved own entries": u["self_approved"],
        "Cash and revenue entries": u["cash_revenue"],
        "Main statement lines": ", ".join(u["lines_touched"])} for u in r["by_user"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        about.to_excel(xl, sheet_name="About", index=False)
        people.to_excel(xl, sheet_name="By user", index=False)
        for c in r["checks"]:
            if not c["count"]:
                continue
            pd.DataFrame([{"Entry": i["entry_id"], "Row": i["row"], "Date": i["date"],
                           "User": i["user"], "Approved by": i["approver"],
                           "Description": i["description"], "Accounts": i["accounts"],
                           "Debits": i["debits"], "Lines": i["lines"]}
                          for i in c["items"][:MAX_EXPORT]]).to_excel(
                xl, sheet_name=SHEETS[c["check"]], index=False)
        finish_workbook(xl.book)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers=attachment(f"{safe_stem(upload.filename)} - who posted what.xlsx"))
