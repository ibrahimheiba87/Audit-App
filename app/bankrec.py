"""Bank reconciliation: match a bank statement to the cash account in the general ledger.

Amounts are "money in" positive on both sides: a receipt is a debit to cash in the ledger and
a credit (deposit) on the bank statement. Each bank line matches at most one ledger line of
the same amount, dated within a few days of it.

What is left over makes the reconciliation at the period end:
- ledger items not on the bank statement by the period end (deposits in transit and
  outstanding payments), with the bank line that cleared each one after the period end, if any;
- bank items not in the ledger (bank charges, interest, direct debits) that need booking.
"""

from __future__ import annotations

import calendar
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .exports import finish_workbook
from .statements import suggest_line

# Ledger lines that carry a balance forward rather than a movement through the bank.
OPENING = re.compile(r"\b(?:opening\s+balance|balance\s+(?:b/?f|brought\s+forward)"
                     r"|brought\s+forward|b/f|b/fwd)\b", re.IGNORECASE)
STALE_DAYS = 90       # an outstanding item this old at the period end is worth a question
CLEAR_DAYS = 60       # how long after the period end a bank line can clear an item
SHOWN_ROWS = 1000     # rows kept per list in a saved result
EARLY_DAYS = 31       # bank-only lines this early in the period may be last year's items


class BankRecError(ValueError):
    """Raised when the files or settings can't be reconciled."""


@dataclass
class Settings:
    period_end: date
    accounts: list[str]
    window_days: int = 5
    bank_balance: float | None = None
    book_balance: float | None = None
    flip_bank: bool = False
    period_start: date | None = None  # a year before the period end when not given


def year_before(d: date) -> date:
    """The day a year before (a month end stays a month end)."""
    last = calendar.monthrange(d.year, d.month)[1]
    new_last = calendar.monthrange(d.year - 1, d.month)[1]
    return date(d.year - 1, d.month, new_last if d.day == last else min(d.day, new_last))


def _cents(x: float) -> int:
    return int(round(float(x) * 100))


def account_code(account: str) -> str:
    """The leading code of a ledger account like "1000 Cash", or the whole text when it
    doesn't start with one ("Cash at bank")."""
    m = re.match(r"\s*([A-Za-z]*\d[\w.\-]*)(?:\s|$)", account or "")
    return m.group(1) if m else (account or "").strip()


def cash_accounts(gl_accounts: list[str], mapped_cash: set[str]) -> list[str]:
    """Ledger accounts that look like cash at bank: mapped to the cash line, or named so."""
    out = []
    for a in gl_accounts:
        code = account_code(a)
        if a in mapped_cash or code in mapped_cash:
            out.append(a)
            continue
        has_code = code != a.strip()
        rest = a.strip()[len(code):].strip() if has_code else a
        if rest and suggest_line(code if has_code else "", rest) == "cash":
            out.append(a)
    return out


def bank_frame(df: pd.DataFrame, signed_amount: bool, flip: bool) -> pd.DataFrame:
    """Bank lines as date, description, money-in amount.

    A statement with Debit and Credit columns is written from the bank's side: a credit is
    money in, so the loader's debit-minus-credit amount is turned round. A single signed
    Amount column is usually money in positive already. `flip` turns it round again for a
    file that does it the other way.
    """
    out = pd.DataFrame({
        "row": df["row"],
        "date": df["date"] if "date" in df else pd.NaT,
        "description": df["description"] if "description" in df else "",
        "amount": df["amount"] if signed_amount else -df["amount"],
    })
    if flip:
        out["amount"] = -out["amount"]
    return out


def ledger_frame(df: pd.DataFrame, accounts: list[str]) -> pd.DataFrame:
    if "account" not in df:
        raise BankRecError("The general ledger has no account column.")
    lines = df[df["account"].isin(accounts)]
    return pd.DataFrame({
        "row": lines["row"],
        "date": lines["date"] if "date" in lines else pd.NaT,
        "entry_id": lines["entry_id"] if "entry_id" in lines else None,
        "description": lines["description"] if "description" in lines else "",
        "amount": lines["amount"],
    })


def _day(value) -> date | None:
    return None if pd.isna(value) else pd.Timestamp(value).date()


def _text(value) -> str | None:
    return None if value is None or pd.isna(value) else str(value)


def reconcile(bank: pd.DataFrame, ledger: pd.DataFrame, s: Settings) -> dict:
    if not s.accounts:
        raise BankRecError("Pick the cash account (or accounts) in the general ledger.")
    if not 0 <= s.window_days <= 60:
        raise BankRecError("The date window must be between 0 and 60 days.")
    pe = s.period_end
    bank = bank.assign(day=bank["date"].map(_day), cents=bank["amount"].map(_cents))
    ledger = ledger.assign(day=ledger["date"].map(_day), cents=ledger["amount"].map(_cents))
    undated_bank = int(bank["day"].isna().sum())
    bank = bank[bank["day"].notna() & (bank["cents"] != 0)]
    opening = ledger["description"].fillna("").astype(str).str.contains(OPENING)
    skipped_opening = int(opening.sum())
    undated_ledger = int((ledger["day"].isna() & ~opening).sum())
    ledger = ledger[ledger["day"].notna() & ~opening & (ledger["cents"] != 0)]
    after_ledger = int((ledger["day"] > pe).sum())
    ledger = ledger[ledger["day"] <= pe].sort_values(["day", "row"])
    if ledger.empty:
        raise BankRecError("No ledger lines for those accounts up to the period end.")
    if bank.empty:
        raise BankRecError("The bank statement has no dated lines with an amount.")

    by_cents: dict[int, list] = {}
    for b in bank.sort_values(["day", "row"]).itertuples():
        by_cents.setdefault(b.cents, []).append(b)
    used: set[int] = set()

    def take(candidates, ok, key):
        best = min((b for b in candidates if b.row not in used and ok(b)), key=key, default=None)
        if best is not None:
            used.add(best.row)
        return best

    window = timedelta(days=s.window_days)
    matched, left = [], []
    for g in ledger.itertuples():
        b = take(by_cents.get(g.cents, ()),
                 lambda b, g=g: b.day <= pe and abs(b.day - g.day) <= window,
                 lambda b, g=g: (abs((b.day - g.day).days), b.day))
        if b is None:
            left.append(g)
            continue
        matched.append({
            "bank_row": int(b.row), "bank_date": b.day.isoformat(),
            "bank_description": _text(b.description),
            "ledger_row": int(g.row), "ledger_date": g.day.isoformat(),
            "entry_id": _text(g.entry_id), "ledger_description": _text(g.description),
            "amount": round(g.cents / 100, 2), "days": (b.day - g.day).days,
        })

    outstanding = []
    last_clear = pe + timedelta(days=CLEAR_DAYS)
    for g in left:
        # On the statement after the period end: still outstanding at the period end, and
        # the later bank line is the evidence that it was real.
        b = take(by_cents.get(g.cents, ()),
                 lambda b, g=g: pe < b.day <= last_clear and b.day >= g.day - window,
                 lambda b: b.day)
        age = (pe - g.day).days
        outstanding.append({
            "row": int(g.row), "date": g.day.isoformat(), "entry_id": _text(g.entry_id),
            "description": _text(g.description), "amount": round(g.cents / 100, 2),
            "kind": "Deposit in transit" if g.cents > 0 else "Outstanding payment",
            "days_outstanding": age,
            "cleared": {"row": int(b.row), "date": b.day.isoformat(),
                        "description": _text(b.description)} if b is not None else None,
            "stale": b is None and age > STALE_DAYS,
        })

    # Bank lines before the period are last year's; at the start of the period an unmatched
    # line may be an item outstanding at last year's end, which is in last year's books.
    start = s.period_start or year_before(pe) + timedelta(days=1)
    before = bank[(bank["day"] < start) & ~bank["row"].isin(used)]
    in_period = bank[(bank["day"] <= pe) & (bank["day"] >= start)]
    early = start + timedelta(days=EARLY_DAYS)
    bank_only = [{
        "row": int(b.row), "date": b.day.isoformat(), "description": _text(b.description),
        "amount": round(b.cents / 100, 2),
        "kind": "Receipt not in the books" if b.cents > 0 else "Payment not in the books",
        "early": b.day < early,
    } for b in in_period.sort_values(["day", "row"]).itertuples() if b.row not in used]
    later_unmatched = int(((bank["day"] > pe) & ~bank["row"].isin(used)).sum())

    def total(items, sign):
        return round(sum(i["amount"] for i in items if (i["amount"] > 0) == (sign > 0)) * sign, 2)

    dit, op = total(outstanding, 1), total(outstanding, -1)
    rnib, pnib = total(bank_only, 1), total(bank_only, -1)
    adjusted_bank = None if s.bank_balance is None else round(s.bank_balance + dit - op, 2)
    adjusted_book = None if s.book_balance is None else round(s.book_balance + rnib - pnib, 2)
    difference = (None if adjusted_bank is None or adjusted_book is None
                  else round(adjusted_bank - adjusted_book, 2))
    return {
        "settings": {"period_start": start.isoformat(),
                     "period_end": pe.isoformat(), "accounts": s.accounts,
                     "window_days": s.window_days, "bank_balance": s.bank_balance,
                     "book_balance": s.book_balance, "flip_bank": s.flip_bank},
        "summary": {
            "bank_lines": len(in_period), "ledger_lines": len(ledger),
            "matched": len(matched),
            "matched_value": round(sum(abs(m["amount"]) for m in matched), 2),
            "outstanding": len(outstanding),
            "cleared_after": sum(1 for o in outstanding if o["cleared"]),
            "stale": sum(1 for o in outstanding if o["stale"]),
            "bank_only": len(bank_only),
            "bank_after_period_unmatched": later_unmatched,
            "bank_before_period": len(before),
            "ledger_after_period": after_ledger, "opening_lines_skipped": skipped_opening,
            "undated_bank": undated_bank, "undated_ledger": undated_ledger,
        },
        "statement": {
            "bank_balance": s.bank_balance, "deposits_in_transit": dit,
            "outstanding_payments": op, "adjusted_bank": adjusted_bank,
            "book_balance": s.book_balance, "receipts_not_in_books": rnib,
            "payments_not_in_books": pnib, "adjusted_book": adjusted_book,
            "difference": difference,
        },
        "matched": matched[:SHOWN_ROWS],
        "outstanding": outstanding[:SHOWN_ROWS],
        "bank_only": bank_only[:SHOWN_ROWS],
    }


EARLY_NOTE = "Early in the period: may be last year's outstanding item"

STATEMENT_ROWS = [
    ("bank_balance", "Balance per bank statement"),
    ("deposits_in_transit", "Add: deposits in transit"),
    ("outstanding_payments", "Less: outstanding payments"),
    ("adjusted_bank", "Adjusted bank balance"),
    ("book_balance", "Balance per books"),
    ("receipts_not_in_books", "Add: receipts on the bank statement not in the books"),
    ("payments_not_in_books", "Less: payments on the bank statement not in the books"),
    ("adjusted_book", "Adjusted book balance"),
    ("difference", "Unexplained difference"),
]


def to_excel(result: dict, context: dict | None = None) -> bytes:
    st, s = result["statement"], result["summary"]
    summary = list((context or {}).items()) + [
        ("Cash accounts", ", ".join(result["settings"]["accounts"])),
        ("Date window (days)", result["settings"]["window_days"]),
        ("Bank lines to the period end", s["bank_lines"]),
        ("Ledger lines to the period end", s["ledger_lines"]),
        ("Matched", s["matched"]),
        ("Outstanding at the period end", s["outstanding"]),
        ("Of which cleared after the period end", s["cleared_after"]),
        ("On the bank statement, not in the books", s["bank_only"]),
        ("", ""),
    ] + [(label, st[key]) for key, label in STATEMENT_ROWS]
    outstanding = pd.DataFrame([{
        "Type": o["kind"], "Date": o["date"], "Entry": o["entry_id"],
        "Description": o["description"], "Amount": o["amount"],
        "Days outstanding": o["days_outstanding"],
        "Cleared on": o["cleared"]["date"] if o["cleared"] else "",
        "Cleared by": o["cleared"]["description"] if o["cleared"] else "",
        "Follow up": "Old and not cleared" if o["stale"] else "",
        "Ledger row": o["row"],
    } for o in result["outstanding"]])
    bank_only = pd.DataFrame([{
        "Type": b["kind"], "Date": b["date"], "Description": b["description"],
        "Amount": b["amount"],
        "Note": EARLY_NOTE if b.get("early") else "", "Bank row": b["row"],
    } for b in result["bank_only"]])
    matched = pd.DataFrame([{
        "Bank date": m["bank_date"], "Bank description": m["bank_description"],
        "Ledger date": m["ledger_date"], "Entry": m["entry_id"],
        "Ledger description": m["ledger_description"], "Amount": m["amount"],
        "Days apart": m["days"], "Bank row": m["bank_row"], "Ledger row": m["ledger_row"],
    } for m in result["matched"]])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(summary, columns=["Item", "Value"]).to_excel(
            xl, sheet_name="Reconciliation", index=False)
        for name, frame in (("Outstanding items", outstanding), ("Bank only", bank_only),
                            ("Matched", matched)):
            (frame if not frame.empty else pd.DataFrame({"": ["None"]})).to_excel(
                xl, sheet_name=name, index=False)
        finish_workbook(xl.book)
    return buf.getvalue()
