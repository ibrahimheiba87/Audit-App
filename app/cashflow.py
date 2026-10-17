"""A draft cash flow statement by the indirect method, from this year's and last year's
statement lines, as in Caseware's financial statements.

The trial balance is before closing entries, so last year's profit is not yet in retained
earnings. Each figure is worked out from the movement in a line:

- profit before tax, with depreciation and finance costs added back;
- the movements in receivables, inventories, other current assets, payables and accruals;
- tax paid: the tax charge less the increase in tax payable;
- investing: the movement in property, plant and equipment after depreciation, and the
  movements in intangible and other non-current assets;
- financing: the movements in borrowings, share capital and other non-current liabilities,
  interest paid (taken as the finance costs) and dividends: last year's equity less this
  year's opening reserves.

The total is checked against the movement in cash; anything left over is shown, as it means
a line is mapped differently from last year or the balance sheet doesn't balance. It is a
draft for the auditor to check against the client's own cash flow statement.
"""

from __future__ import annotations

CENT = 0.005


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def line_totals(data: dict) -> dict[str, tuple[float, float]]:
    """Each statement line's (this year, last year), shown on its normal side."""
    return {s["line"]: (s["totals"]["adjusted"], s["totals"]["prior"])
            for s in data["lead_sheets"]}


def compute(data: dict) -> dict | None:
    """The cash flow statement, or None without last year's trial balance."""
    if not data.get("has_prior"):
        return None
    t = line_totals(data)

    def cur(line: str) -> float:
        return t.get(line, (0.0, 0.0))[0]

    def pri(line: str) -> float:
        return t.get(line, (0.0, 0.0))[1]

    def up(line: str) -> float:
        return cur(line) - pri(line)

    st = data["statements"]
    pbt = next(r["current"] for r in st["income_statement"] if r["label"] == "Profit before tax")
    rows: list[dict] = []

    def add(label: str, amount: float, kind: str = "line") -> float:
        rows.append({"label": label, "amount": _r(amount), "kind": kind})
        return amount

    def heading(label: str) -> None:
        rows.append({"label": label, "amount": None, "kind": "heading"})

    heading("Cash flows from operating activities")
    op = add("Profit before tax", pbt)
    op += add("Depreciation and amortisation", cur("depreciation"))
    op += add("Finance costs", cur("finance_costs"))
    for line, label in (("receivables", "(Increase) / decrease in trade and other receivables"),
                        ("inventory", "(Increase) / decrease in inventories"),
                        ("other_current_assets",
                         "(Increase) / decrease in prepayments and other current assets")):
        if line in t:
            op += add(label, -up(line))
    for line, label in (("payables", "Increase / (decrease) in trade and other payables"),
                        ("accruals",
                         "Increase / (decrease) in accruals and other current liabilities")):
        if line in t:
            op += add(label, up(line))
    op += add("Income tax paid", -(cur("income_tax") - up("tax_payable")))
    add("Net cash from operating activities", op, "subtotal")

    heading("Cash flows from investing activities")
    inv = add("Purchase of property, plant and equipment (net)",
              -(up("ppe") + cur("depreciation")))
    if "intangibles" in t:
        inv += add("Purchase of intangible assets (net)", -up("intangibles"))
    if "other_noncurrent_assets" in t:
        inv += add("Movement in other non-current assets", -up("other_noncurrent_assets"))
    add("Net cash used in investing activities", inv, "subtotal")

    heading("Cash flows from financing activities")
    fin = 0.0
    if "borrowings" in t or "borrowings_current" in t:
        fin += add("Proceeds from / (repayment of) borrowings",
                   up("borrowings") + up("borrowings_current"))
    if "share_capital" in t:
        fin += add("Proceeds from issue of shares", up("share_capital"))
    if "other_noncurrent_liabilities" in t:
        fin += add("Movement in other non-current liabilities",
                   up("other_noncurrent_liabilities"))
    fin += add("Interest paid", -cur("finance_costs"))
    # Last year's closing equity (reserves plus last year's profit) less this year's opening
    # reserves: what left equity in between, usually dividends.
    dividends = pri("retained_earnings") + st["profit"]["prior"] - cur("retained_earnings")
    fin += add("Dividends paid and other movements in reserves", -dividends)
    add("Net cash from financing activities", fin, "subtotal")

    total = op + inv + fin
    add("Net increase / (decrease) in cash", total, "total")
    add("Cash at the beginning of the year", pri("cash"))
    add("Cash at the end of the year", pri("cash") + total, "total")
    difference = _r(cur("cash") - (pri("cash") + total))
    notes = []
    if abs(difference) >= CENT:
        notes.append(f"The cash flows don't add up to the movement in cash: {difference:,.2f} "
                     "is left over. Check that accounts are mapped to the same lines as last "
                     "year and that both balance sheets balance.")
    if "cash" not in t:
        notes.append("No account is mapped to cash and cash equivalents.")
    return {"rows": rows, "operating": _r(op), "investing": _r(inv), "financing": _r(fin),
            "net_change": _r(total), "cash_opening": _r(pri("cash")),
            "cash_closing": _r(cur("cash")), "difference": difference, "notes": notes}


def sheet(cf: dict | None) -> dict[str, list[dict]]:
    """The cash flow statement as a sheet for the lead sheets workbook."""
    if cf is None:
        return {}
    rows = [{"Line": r["label"], "Amount": r["amount"]} for r in cf["rows"]]
    rows += [{"Line": "Difference to the movement in cash", "Amount": cf["difference"]}]
    rows += [{"Line": n, "Amount": None} for n in cf["notes"]]
    return {"Cash flow": rows}
