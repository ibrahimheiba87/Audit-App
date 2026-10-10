"""Prior-year comparison: account balances this year vs last year, with variance flags."""

from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd

from .exports import finish_workbook


@dataclass
class CompareSettings:
    threshold_amount: float  # flag changes at least this large...
    threshold_pct: float = 10.0  # ...that are also at least this % of the prior balance


class CompareError(ValueError):
    pass


def account_balances(df: pd.DataFrame) -> pd.DataFrame:
    """Net amount and a display name per account (a TB has one line each; a GL is summed)."""
    if "account" not in df:
        raise CompareError("Both files need an account column to compare balances.")
    accounts = df["account"].fillna("(blank)")
    out = df.assign(account=accounts).groupby("account", sort=False).agg(balance=("amount", "sum"))
    if "description" in df:
        names = df.assign(account=accounts).dropna(subset=["description"])
        out["name"] = names.groupby("account")["description"].first()
    else:
        out["name"] = None
    return out


def _pct(change: float, prior: float) -> float | None:
    """Change as a % of the prior balance, so a credit balance that grows reads as positive."""
    return None if abs(prior) < 0.005 else round(change / prior * 100, 1)


def compare(current: pd.DataFrame, prior: pd.DataFrame, s: CompareSettings) -> dict:
    if s.threshold_amount <= 0:
        raise CompareError("Threshold amount must be more than zero.")
    if s.threshold_pct < 0:
        raise CompareError("Threshold percentage can't be negative.")
    cur = account_balances(current)
    pri = account_balances(prior)
    joined = cur.join(pri, how="outer", lsuffix="_cur", rsuffix="_pri")

    rows = []
    for account, r in joined.iterrows():
        c = 0.0 if pd.isna(r["balance_cur"]) else float(r["balance_cur"])
        p = 0.0 if pd.isna(r["balance_pri"]) else float(r["balance_pri"])
        name = r["name_cur"] if isinstance(r["name_cur"], str) else r["name_pri"]
        change = c - p
        pct = _pct(change, p)
        reasons = []
        if pd.isna(r["balance_pri"]) and abs(c) >= 0.005:
            reasons.append("New account this year")
        elif pd.isna(r["balance_cur"]) and abs(p) >= 0.005:
            reasons.append("Account not in this year's file")
        big = abs(change) >= s.threshold_amount
        if big and (pct is None or abs(pct) >= s.threshold_pct):
            reasons.append("Change at or above threshold")
        if p and c and (p > 0) != (c > 0) and abs(change) >= s.threshold_amount:
            reasons.append("Balance switched between debit and credit")
        rows.append({
            "account": str(account),
            "name": name if isinstance(name, str) else None,
            "current": round(c, 2),
            "prior": round(p, 2),
            "change": round(change, 2),
            "change_pct": pct,
            "flagged": bool(reasons),
            "reasons": reasons,
        })

    rows.sort(key=lambda x: (not x["flagged"], -abs(x["change"])))
    return {
        "settings": {"threshold_amount": s.threshold_amount, "threshold_pct": s.threshold_pct},
        "summary": {
            "accounts": len(rows),
            "flagged": sum(r["flagged"] for r in rows),
            "new_accounts": sum("New account this year" in r["reasons"] for r in rows),
            "removed_accounts": sum("Account not in this year's file" in r["reasons"]
                                    for r in rows),
            "current_total": round(float(cur["balance"].sum()), 2),
            "prior_total": round(float(pri["balance"].sum()), 2),
        },
        "rows": rows,
    }


def to_excel(result: dict, context: dict | None = None) -> bytes:
    s = result["summary"]
    summary = list((context or {}).items()) + [
        ("Threshold amount", result["settings"]["threshold_amount"]),
        ("Threshold %", result["settings"]["threshold_pct"]),
        ("Accounts compared", s["accounts"]),
        ("Accounts flagged", s["flagged"]),
        ("New accounts", s["new_accounts"]),
        ("Accounts no longer in file", s["removed_accounts"]),
        ("Current total (should be 0 for a TB)", s["current_total"]),
        ("Prior total (should be 0 for a TB)", s["prior_total"]),
    ]
    table = pd.DataFrame([{
        "Account": r["account"], "Name": r["name"], "Current": r["current"],
        "Prior": r["prior"], "Change": r["change"], "Change %": r["change_pct"],
        "Flagged": "Yes" if r["flagged"] else "", "Reasons": "; ".join(r["reasons"]),
    } for r in result["rows"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(summary, columns=["Item", "Value"]).to_excel(xl, sheet_name="Summary", index=False)
        table.to_excel(xl, sheet_name="Comparison", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()
