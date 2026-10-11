"""Lead sheets, adjusting entries and draft financial statements from a trial balance.

Every trial balance account is mapped to a financial statement line. Adjusting entries are
added to the unadjusted balances to give the adjusted trial balance, which rolls up into lead
sheets (one per line) and draft statements. Proposed entries the client has not booked stay
out of the adjusted balances and are summarised against materiality instead.

Amounts are signed debit-positive throughout, as the loader gives them. For display, each
line shows its balance on its normal side (assets and expenses as debits, the rest as
credits) as a positive number.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass

import pandas as pd

from .exports import finish_workbook


@dataclass(frozen=True)
class Section:
    id: str
    label: str
    statement: str  # "bs" or "is"
    debit_normal: bool


SECTIONS = [
    Section("current_assets", "Current assets", "bs", True),
    Section("noncurrent_assets", "Non-current assets", "bs", True),
    Section("current_liabilities", "Current liabilities", "bs", False),
    Section("noncurrent_liabilities", "Non-current liabilities", "bs", False),
    Section("equity", "Equity", "bs", False),
    Section("revenue", "Revenue", "is", False),
    Section("cost_of_sales", "Cost of sales", "is", True),
    Section("operating_expenses", "Operating expenses", "is", True),
    Section("other_income", "Other income", "is", False),
    Section("finance_costs", "Finance costs", "is", True),
    Section("income_tax", "Income tax", "is", True),
]
SECTION = {s.id: s for s in SECTIONS}

# (id, label, section)
LINES = [
    ("cash", "Cash and cash equivalents", "current_assets"),
    ("receivables", "Trade and other receivables", "current_assets"),
    ("inventory", "Inventories", "current_assets"),
    ("other_current_assets", "Prepayments and other current assets", "current_assets"),
    ("ppe", "Property, plant and equipment", "noncurrent_assets"),
    ("intangibles", "Intangible assets", "noncurrent_assets"),
    ("other_noncurrent_assets", "Other non-current assets", "noncurrent_assets"),
    ("payables", "Trade and other payables", "current_liabilities"),
    ("accruals", "Accruals and other current liabilities", "current_liabilities"),
    ("tax_payable", "Current tax payable", "current_liabilities"),
    ("borrowings_current", "Short-term borrowings", "current_liabilities"),
    ("borrowings", "Long-term borrowings", "noncurrent_liabilities"),
    ("other_noncurrent_liabilities", "Other non-current liabilities", "noncurrent_liabilities"),
    ("share_capital", "Share capital", "equity"),
    ("retained_earnings", "Retained earnings and reserves", "equity"),
    ("revenue", "Revenue", "revenue"),
    ("cost_of_sales", "Cost of sales", "cost_of_sales"),
    ("staff_costs", "Employee costs", "operating_expenses"),
    ("premises", "Rent and premises", "operating_expenses"),
    ("depreciation", "Depreciation and amortisation", "operating_expenses"),
    ("admin", "Administrative expenses", "operating_expenses"),
    ("other_expenses", "Other operating expenses", "operating_expenses"),
    ("other_income", "Other income", "other_income"),
    ("finance_costs", "Finance costs", "finance_costs"),
    ("income_tax", "Income tax expense", "income_tax"),
]
LINE = {id_: {"id": id_, "label": label, "section": section} for id_, label, section in LINES}

ENTRY_KINDS = {
    "adjusting": {"label": "Adjusting", "prefix": "AJE", "booked": True},
    "reclassification": {"label": "Reclassification", "prefix": "RJE", "booked": True},
    "proposed": {"label": "Proposed (not booked)", "prefix": "PAJE", "booked": False},
}

# Name keywords, checked in order, so "bank loan" is a borrowing before "bank" is cash and
# "common stock" is share capital before "stock" is inventory. Each keyword matches a whole
# word, also in the plural, so "rent" doesn't match "current" and "stock" doesn't match
# "stockholders". A trailing * marks a stem: "inventor*" matches "inventory", "inventories".
KEYWORDS = [
    (("accumulated depreciation", "accumulated amortisation", "accumulated amortization"), "ppe"),
    (("depreciation", "amortisation", "amortization"), "depreciation"),
    (("accrued income", "accrued revenue"), "other_current_assets"),
    (("income tax", "tax expense", "corporation tax"), "income_tax"),
    (("tax payable", "taxes payable", "vat", "gst", "sales tax"), "tax_payable"),
    (("receivable", "debtor"), "receivables"),
    (("loan", "borrowing", "mortgage", "overdraft", "notes payable", "debenture", "lease liab*",
      "debt"), "borrowings"),
    (("interest expense", "bank charge", "finance cost", "bank fee"), "finance_costs"),
    (("interest income", "dividend income", "other income", "gain on", "sundry income"),
     "other_income"),
    (("cash", "bank", "petty"), "cash"),
    (("share capital", "common stock", "capital stock", "preferred stock", "share premium",
      "paid-in", "paid in"), "share_capital"),
    (("inventor*", "stock"), "inventory"),
    (("customer deposit", "liabilit*"), "accruals"),
    (("prepaid", "prepayment", "deposit"), "other_current_assets"),
    (("propert*", "plant", "equipment", "furniture", "vehicle", "building", "land",
      "computer", "fixed asset", "machinery"), "ppe"),
    (("goodwill", "intangible", "software", "patent", "trademark", "licence", "license"),
     "intangibles"),
    (("payable", "creditor"), "payables"),
    (("accrued", "accrual", "deferred revenue", "unearned", "provision"), "accruals"),
    (("retained", "reserve", "dividend"), "retained_earnings"),
    (("cost of sales", "cost of goods", "cogs", "purchases", "cost of revenue"),
     "cost_of_sales"),
    (("revenue", "sales", "turnover", "fees earned", "service income"), "revenue"),
    (("salar*", "wage", "payroll", "staff", "employee", "pension", "bonus"), "staff_costs"),
    (("rent", "premises", "utilities", "electricity", "occupancy"), "premises"),
    (("office", "admin*", "postage", "telephone", "insurance", "professional", "audit fee",
      "legal", "consult*", "subscription", "stationery", "supplies"), "admin"),
    (("expense", "travel*", "advertising", "marketing", "repairs", "entertainment"),
     "other_expenses"),
]


def _keyword(word: str) -> str:
    if word.endswith("*"):
        return r"\b" + re.escape(word[:-1])
    return r"\b" + re.escape(word) + r"(?:s|es)?\b"


KEYWORD_PATTERNS = [(re.compile("|".join(_keyword(w) for w in words)), line)
                    for words, line in KEYWORDS]
# First digit of a numeric account code, as in most charts of accounts.
DIGIT = {"1": "other_current_assets", "2": "accruals", "3": "retained_earnings",
         "4": "revenue", "5": "cost_of_sales", "6": "other_expenses", "7": "other_expenses"}


class StatementsError(ValueError):
    pass


def suggest_line(account: str, name: str | None) -> str | None:
    """A starting FS line from the account's name (or the account itself), else its number."""
    text = (name or account).lower()
    for pattern, line in KEYWORD_PATTERNS:
        if pattern.search(text):
            return line
    code = re.match(r"\s*(\d)", account)
    return DIGIT.get(code.group(1)) if code else None


def debit_normal(line_id: str | None) -> bool:
    return SECTION[LINE[line_id]["section"]].debit_normal if line_id in LINE else True


def shown(amount: float, line_id: str | None) -> float:
    """A debit-positive amount as shown on its line (positive on the line's normal side)."""
    return _r(amount if debit_normal(line_id) else -amount)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def check_entry_lines(lines: list[dict]) -> None:
    """An entry needs at least two lines, each a debit or a credit, that balance."""
    if len(lines) < 2:
        raise StatementsError("An entry needs at least two lines.")
    for i, ln in enumerate(lines, start=1):
        dr, cr = ln.get("debit") or 0.0, ln.get("credit") or 0.0
        if not all(math.isfinite(v) and v >= 0 for v in (dr, cr)):
            raise StatementsError(f"Line {i}: amounts must be numbers of zero or more.")
        if (dr > 0) == (cr > 0):
            raise StatementsError(f"Line {i}: enter either a debit or a credit.")
        if not str(ln.get("account") or "").strip():
            raise StatementsError(f"Line {i}: enter an account.")
    try:  # in whole cents, so long entries of large amounts still compare exactly
        debit_cents = sum(round((ln.get("debit") or 0.0) * 100) for ln in lines)
        credit_cents = sum(round((ln.get("credit") or 0.0) * 100) for ln in lines)
    except OverflowError:
        raise StatementsError("The entry's amounts are too large.") from None
    debits, credits = debit_cents / 100, credit_cents / 100
    if debit_cents != credit_cents:
        raise StatementsError(f"Debits ({debits:,.2f}) and credits ({credits:,.2f}) "
                              "must be equal.")


def entry_effects(entry: dict) -> dict[str, float]:
    """Net debit-positive change per account from one entry."""
    out: dict[str, float] = {}
    for ln in entry["lines"]:
        out[ln["account"]] = out.get(ln["account"], 0.0) + (ln.get("debit") or 0.0) \
            - (ln.get("credit") or 0.0)
    return out


def _balances(df: pd.DataFrame | None) -> dict[str, dict]:
    """account -> {"balance", "name"} from a loaded trial balance."""
    if df is None:
        return {}
    if "account" not in df:
        raise StatementsError("The trial balance needs an account column.")
    accounts = df["account"].astype("string").str.strip().fillna("").replace("", "(blank)")
    names = (df["description"].astype("string").str.strip().replace("", pd.NA)
             if "description" in df else pd.Series(pd.NA, index=df.index, dtype="string"))
    grouped = pd.DataFrame({"account": accounts, "amount": df["amount"].astype(float),
                            "name": names}).groupby("account", sort=False)
    balances, first_names = grouped["amount"].sum(), grouped["name"].first()
    return {a: {"balance": float(balances[a]),
                "name": None if pd.isna(first_names[a]) else str(first_names[a])}
            for a in balances.index}


def build(tb: pd.DataFrame, prior: pd.DataFrame | None, mapping: dict[str, str],
          entries: list[dict], materiality: dict | None = None) -> dict:
    """Adjusted trial balance, lead sheets, draft statements and uncorrected misstatements."""
    cur, pri = _balances(tb), _balances(prior)
    adjusting: dict[str, float] = {}
    reclass: dict[str, float] = {}
    for e in entries:
        target = {"adjusting": adjusting, "reclassification": reclass}.get(e["kind"])
        if target is not None:
            for account, amount in entry_effects(e).items():
                target[account] = target.get(account, 0.0) + amount

    accounts = list(dict.fromkeys([*cur, *adjusting, *reclass, *pri]))
    names = {a: (cur.get(a) or pri.get(a) or {}).get("name") for a in accounts}
    rows = []
    for a in accounts:
        line = mapping.get(a)
        unadj = cur.get(a, {}).get("balance", 0.0)
        adj, rje = adjusting.get(a, 0.0), reclass.get(a, 0.0)
        rows.append({
            "account": a, "name": names[a], "line": line if line in LINE else None,
            "suggested": None if line in LINE else suggest_line(a, names[a]),
            "in_tb": a in cur, "unadjusted": _r(unadj), "adjusting": _r(adj),
            "reclassification": _r(rje), "adjusted": _r(unadj + adj + rje),
            "prior": _r(pri[a]["balance"]) if a in pri else None,
        })

    by_line: dict[str, list[dict]] = {}
    for r in rows:
        if r["line"]:
            by_line.setdefault(r["line"], []).append(r)
    lead_sheets = []
    for line_id, label, section in LINES:
        accts = by_line.get(line_id, [])
        if not accts:
            continue
        sheet_rows = [{
            "account": r["account"], "name": r["name"],
            **{k: shown(r[k], line_id) for k in ("unadjusted", "adjusting",
                                                   "reclassification", "adjusted")},
            "prior": None if r["prior"] is None else shown(r["prior"], line_id),
        } for r in accts]
        totals = {k: _r(sum(x[k] for x in sheet_rows)) for k in
                  ("unadjusted", "adjusting", "reclassification", "adjusted")}
        totals["prior"] = _r(sum(x["prior"] or 0.0 for x in sheet_rows))
        lead_sheets.append({"line": line_id, "label": label, "section": section,
                            "section_label": SECTION[section].label,
                            "accounts": sheet_rows, "totals": totals})

    unmapped = [r for r in rows if r["line"] is None and
                (abs(r["adjusted"]) >= 0.005 or (r["prior"] and abs(r["prior"]) >= 0.005))]
    statements = _statements(by_line)
    return {
        "lines": [LINE[i] for i, _, _ in LINES],
        "sections": [{"id": s.id, "label": s.label, "statement": s.statement} for s in SECTIONS],
        "accounts": rows,
        "lead_sheets": lead_sheets,
        "unmapped": [{"account": r["account"], "name": r["name"], "adjusted": r["adjusted"],
                      "suggested": r["suggested"]} for r in unmapped],
        "statements": statements,
        "totals": {"unadjusted": _r(sum(r["unadjusted"] for r in rows)),
                   "adjusted": _r(sum(r["adjusted"] for r in rows)),
                   "prior": _r(sum(r["prior"] or 0.0 for r in rows)) if pri else None},
        "uncorrected": uncorrected_summary(entries, mapping, materiality),
        "has_prior": bool(pri),
    }


def _statements(by_line: dict[str, list[dict]]) -> dict:
    def section_lines(section: str) -> list[dict]:
        out = []
        for line_id, label, sec in LINES:
            if sec != section:
                continue
            accts = by_line.get(line_id, [])
            if accts:
                cur = sum(shown(r["adjusted"], line_id) for r in accts)
                pri = sum(shown(r["prior"] or 0.0, line_id) for r in accts)
                out.append({"line": line_id, "label": label, "current": _r(cur),
                            "prior": _r(pri)})
        return out

    def total(lines: list[dict]) -> dict:
        return {"current": _r(sum(x["current"] for x in lines)),
                "prior": _r(sum(x["prior"] for x in lines))}

    def row(label, cur, pri, kind="line"):
        return {"label": label, "current": _r(cur), "prior": _r(pri), "kind": kind}

    sec = {s.id: section_lines(s.id) for s in SECTIONS}
    t = {k: total(v) for k, v in sec.items()}

    # Income statement
    inc: list[dict] = []

    def add_section(sid: str, sign: int = 1):
        for x in sec[sid]:
            inc.append(row(x["label"], sign * x["current"], sign * x["prior"]))

    def cum(*parts: tuple[str, int]) -> tuple[float, float]:
        return (sum(sign * t[s]["current"] for s, sign in parts),
                sum(sign * t[s]["prior"] for s, sign in parts))

    add_section("revenue")
    add_section("cost_of_sales", -1)
    gp = cum(("revenue", 1), ("cost_of_sales", -1))
    inc.append(row("Gross profit", *gp, "subtotal"))
    add_section("operating_expenses", -1)
    add_section("other_income")
    op = cum(("revenue", 1), ("cost_of_sales", -1), ("operating_expenses", -1),
             ("other_income", 1))
    inc.append(row("Operating profit", *op, "subtotal"))
    add_section("finance_costs", -1)
    pbt = (op[0] - t["finance_costs"]["current"], op[1] - t["finance_costs"]["prior"])
    inc.append(row("Profit before tax", *pbt, "subtotal"))
    add_section("income_tax", -1)
    profit = (pbt[0] - t["income_tax"]["current"], pbt[1] - t["income_tax"]["prior"])
    inc.append(row("Profit for the year", *profit, "total"))

    # Balance sheet. The trial balance is before closing entries, so this year's profit is
    # shown in equity on its own line.
    bal: list[dict] = []
    for sid in ("noncurrent_assets", "current_assets"):
        bal.append(row(SECTION[sid].label, 0, 0, "heading"))
        bal += [row(x["label"], x["current"], x["prior"]) for x in sec[sid]]
        bal.append(row(f"Total {SECTION[sid].label.lower()}", t[sid]["current"],
                       t[sid]["prior"], "subtotal"))
    assets = cum(("current_assets", 1), ("noncurrent_assets", 1))
    bal.append(row("Total assets", *assets, "total"))
    for sid in ("noncurrent_liabilities", "current_liabilities"):
        bal.append(row(SECTION[sid].label, 0, 0, "heading"))
        bal += [row(x["label"], x["current"], x["prior"]) for x in sec[sid]]
        bal.append(row(f"Total {SECTION[sid].label.lower()}", t[sid]["current"],
                       t[sid]["prior"], "subtotal"))
    liabilities = cum(("current_liabilities", 1), ("noncurrent_liabilities", 1))
    bal.append(row("Total liabilities", *liabilities, "subtotal"))
    bal.append(row("Equity", 0, 0, "heading"))
    bal += [row(x["label"], x["current"], x["prior"]) for x in sec["equity"]]
    bal.append(row("Profit for the year", *profit))
    equity = (t["equity"]["current"] + profit[0], t["equity"]["prior"] + profit[1])
    bal.append(row("Total equity", *equity, "subtotal"))
    bal.append(row("Total liabilities and equity", liabilities[0] + equity[0],
                   liabilities[1] + equity[1], "total"))
    difference = (assets[0] - liabilities[0] - equity[0], assets[1] - liabilities[1] - equity[1])
    return {
        "income_statement": inc,
        "balance_sheet": bal,
        "profit": {"current": _r(profit[0]), "prior": _r(profit[1])},
        "difference": {"current": _r(difference[0]), "prior": _r(difference[1])},
    }


def uncorrected_summary(entries: list[dict], lines: dict[str, str | None],
                        materiality: dict | None) -> dict:
    """Proposed entries the client has not booked, and their effect against materiality.

    Only saved mappings count: an account that isn't mapped yet is listed as unclassified and
    there is no conclusion until it is, rather than guessing its line."""
    items = []
    unclassified: dict[str, None] = {}
    for e in entries:
        if e["kind"] != "proposed":
            continue
        profit = assets = liabilities = 0.0
        for account, amount in entry_effects(e).items():
            line = lines.get(account)
            section = LINE.get(line or "", {}).get("section")
            statement = SECTION[section].statement if section else None
            if section is None:
                if abs(amount) >= 0.005:
                    unclassified[account] = None
            elif statement == "is":
                profit -= amount  # a debit to an income statement account reduces profit
            elif section in ("current_assets", "noncurrent_assets"):
                assets += amount
            elif section in ("current_liabilities", "noncurrent_liabilities"):
                liabilities -= amount
        items.append({"id": e["id"], "ref": e["ref"], "description": e["description"],
                      "profit": _r(profit), "assets": _r(assets),
                      "liabilities": _r(liabilities), "net_assets": _r(assets - liabilities)})
    profit_total = _r(sum(i["profit"] for i in items))
    out = {"entries": items, "profit": profit_total,
           "net_assets": _r(sum(i["net_assets"] for i in items)),
           "overall_materiality": None, "performance_materiality": None,
           "trivial_threshold": None, "pct_of_overall": None, "conclusion": None,
           "unclassified": list(unclassified)}
    if materiality and materiality.get("overall_materiality"):
        overall = materiality["overall_materiality"]
        trivial = materiality.get("trivial_threshold") or 0.0
        largest = max([abs(profit_total)] + [abs(i["net_assets"]) for i in items] +
                      [abs(sum(i["net_assets"] for i in items))])
        out.update({
            "overall_materiality": overall,
            "performance_materiality": materiality.get("performance_materiality"),
            "trivial_threshold": trivial,
            "pct_of_overall": round(largest / overall * 100, 1),
        })
        if unclassified:
            out["conclusion"] = ("Map " + ", ".join(unclassified) + " to a statement line "
                                 "first: their effect isn't counted yet.")
        elif largest >= overall:
            out["conclusion"] = ("Uncorrected misstatements are material: ask the client to "
                                 "book them, or consider the effect on the audit opinion.")
        elif largest >= (materiality.get("performance_materiality") or overall):
            out["conclusion"] = ("Uncorrected misstatements are above performance "
                                 "materiality: consider asking the client to book them.")
        elif largest > trivial:
            out["conclusion"] = "Uncorrected misstatements are below performance materiality."
        else:
            out["conclusion"] = "Uncorrected misstatements are clearly trivial."
    return out


def to_excel(data: dict, entries: list[dict], context: dict | None = None) -> bytes:
    labels = {i: LINE[i]["label"] for i in LINE}
    st = data["statements"]
    summary = list((context or {}).items()) + [
        ("Unadjusted TB total (should be 0)", data["totals"]["unadjusted"]),
        ("Adjusted TB total (should be 0)", data["totals"]["adjusted"]),
        ("Unmapped accounts", len(data["unmapped"])),
        ("Balance sheet difference", st["difference"]["current"]),
        ("Profit for the year", st["profit"]["current"]),
    ]
    def statement_row(statement: str, r: dict) -> dict:
        figures = r["kind"] != "heading"
        return {"Statement": statement, "Line": r["label"],
                "Current": r["current"] if figures else None,
                "Prior": r["prior"] if figures and data["has_prior"] else None}

    statements = [statement_row("Income statement", r) for r in st["income_statement"]]
    statements += [statement_row("Balance sheet", r) for r in st["balance_sheet"]]
    lead = [{"FS line": s["label"], "Account": a["account"], "Name": a["name"],
             "Unadjusted": a["unadjusted"], "Adjusting": a["adjusting"],
             "Reclassification": a["reclassification"], "Adjusted": a["adjusted"],
             "Prior year": a["prior"]}
            for s in data["lead_sheets"] for a in s["accounts"]]
    tb = [{"Account": r["account"], "Name": r["name"],
           "FS line": labels.get(r["line"], "Unmapped"),
           "Unadjusted": r["unadjusted"], "Adjusting": r["adjusting"],
           "Reclassification": r["reclassification"], "Adjusted": r["adjusted"],
           "Adjusted debit": r["adjusted"] if r["adjusted"] > 0 else None,
           "Adjusted credit": -r["adjusted"] if r["adjusted"] < 0 else None,
           "Prior year": r["prior"]} for r in data["accounts"]]
    je = [{"Entry": e["ref"], "Type": ENTRY_KINDS[e["kind"]]["label"],
           "Description": e["description"], "Date": e.get("date"), "Account": ln["account"],
           "Debit": ln.get("debit") or None, "Credit": ln.get("credit") or None,
           "Memo": ln.get("memo") or "", "By": e.get("author", ""),
           "Created (UTC)": e.get("created_at", "")} for e in entries for ln in e["lines"]]
    u = data["uncorrected"]
    unc = [{"Entry": i["ref"], "Description": i["description"], "Effect on profit": i["profit"],
            "Effect on net assets": i["net_assets"]} for i in u["entries"]]
    unc_summary = [("Total effect on profit", u["profit"]),
                   ("Total effect on net assets", u["net_assets"]),
                   ("Overall materiality", u["overall_materiality"]),
                   ("Performance materiality", u["performance_materiality"]),
                   ("Clearly trivial", u["trivial_threshold"]),
                   ("Largest effect as % of overall materiality", u["pct_of_overall"]),
                   ("Accounts not on a statement line", ", ".join(u["unclassified"]) or None),
                   ("Conclusion", u["conclusion"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(summary, columns=["Item", "Value"]).to_excel(xl, sheet_name="Summary",
                                                                  index=False)
        pd.DataFrame(statements, columns=["Statement", "Line", "Current", "Prior"]).to_excel(
            xl, sheet_name="Statements", index=False)
        pd.DataFrame(lead, columns=["FS line", "Account", "Name", "Unadjusted", "Adjusting",
                                    "Reclassification", "Adjusted", "Prior year"]).to_excel(
            xl, sheet_name="Lead sheets", index=False)
        pd.DataFrame(tb).to_excel(xl, sheet_name="Adjusted TB", index=False)
        pd.DataFrame(je, columns=["Entry", "Type", "Description", "Date", "Account", "Debit",
                                  "Credit", "Memo", "By", "Created (UTC)"]).to_excel(
            xl, sheet_name="Adjusting entries", index=False)
        pd.DataFrame(unc, columns=["Entry", "Description", "Effect on profit",
                                   "Effect on net assets"]).to_excel(
            xl, sheet_name="Uncorrected", index=False)
        pd.DataFrame(unc_summary, columns=["Item", "Value"]).to_excel(
            xl, sheet_name="Uncorrected", index=False, startrow=len(unc) + 3)
        finish_workbook(xl.book)
    return buf.getvalue()
