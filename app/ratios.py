"""Key ratios from the draft statements, this year against last, with warning signs.

Figures come from the lead sheet totals (each line on its normal side, as a positive number),
so the ratios follow the account mapping and the booked adjusting entries.
"""

from __future__ import annotations

import math

from .statements import LINE

DAYS = 365
# A ratio that moves by this share of last year's value is flagged for follow-up.
CHANGE_THRESHOLD = 0.25

GROUPS = {"liquidity": "Liquidity", "profitability": "Profitability",
          "efficiency": "Working capital", "leverage": "Debt and cover"}


def _figures(data: dict, column: str) -> dict[str, float]:
    """Section and line totals for one year ("adjusted" or "prior")."""
    lines: dict[str, float] = {}
    sections: dict[str, float] = {}
    for sheet in data["lead_sheets"]:
        amount = sheet["totals"][column] or 0.0
        lines[sheet["line"]] = lines.get(sheet["line"], 0.0) + amount
        section = LINE[sheet["line"]]["section"]
        sections[section] = sections.get(section, 0.0) + amount
    # Some lines share their section's id (revenue, cost_of_sales, ...); count those once.
    f = {**lines, **sections}
    get = lambda k: f.get(k, 0.0)  # noqa: E731
    gross = get("revenue") - get("cost_of_sales")
    operating = gross - get("operating_expenses") + get("other_income")
    profit = operating - get("finance_costs") - get("income_tax")
    f.update({
        "gross_profit": gross, "operating_profit": operating, "profit": profit,
        "total_assets": get("current_assets") + get("noncurrent_assets"),
        "total_equity": get("equity") + profit,
        "debt": get("borrowings_current") + get("borrowings"),
    })
    return f


def _div(a: float, b: float, scale: float = 1.0) -> float | None:
    if not b or abs(b) < 0.005:
        return None
    value = a / b * scale
    return value if math.isfinite(value) else None


# id, label, group, unit ("x", "%", "days"), formula, what it means
RATIOS = [
    ("current", "Current ratio", "liquidity", "x",
     lambda f: _div(f.get("current_assets", 0), f.get("current_liabilities", 0)),
     "Current assets ÷ current liabilities"),
    ("quick", "Quick ratio", "liquidity", "x",
     lambda f: _div(f.get("current_assets", 0) - f.get("inventory", 0),
                    f.get("current_liabilities", 0)),
     "Current assets less inventories ÷ current liabilities"),
    ("gross_margin", "Gross margin", "profitability", "%",
     lambda f: _div(f["gross_profit"], f.get("revenue", 0), 100),
     "Gross profit ÷ revenue"),
    ("operating_margin", "Operating margin", "profitability", "%",
     lambda f: _div(f["operating_profit"], f.get("revenue", 0), 100),
     "Operating profit ÷ revenue"),
    ("net_margin", "Net margin", "profitability", "%",
     lambda f: _div(f["profit"], f.get("revenue", 0), 100),
     "Profit for the year ÷ revenue"),
    ("return_on_assets", "Return on assets", "profitability", "%",
     lambda f: _div(f["profit"], f["total_assets"], 100),
     "Profit for the year ÷ total assets"),
    ("receivable_days", "Receivable days", "efficiency", "days",
     lambda f: _div(f.get("receivables", 0), f.get("revenue", 0), DAYS),
     "Trade receivables ÷ revenue × 365"),
    ("inventory_days", "Inventory days", "efficiency", "days",
     lambda f: _div(f.get("inventory", 0), f.get("cost_of_sales", 0), DAYS),
     "Inventories ÷ cost of sales × 365"),
    ("payable_days", "Payable days", "efficiency", "days",
     lambda f: _div(f.get("payables", 0), f.get("cost_of_sales", 0), DAYS),
     "Trade payables ÷ cost of sales × 365"),
    ("debt_to_equity", "Debt to equity", "leverage", "x",
     lambda f: _div(f["debt"], f["total_equity"]) if f["total_equity"] > 0 else None,
     "Borrowings ÷ total equity"),
    ("interest_cover", "Interest cover", "leverage", "x",
     lambda f: _div(f["operating_profit"], f.get("finance_costs", 0)),
     "Operating profit ÷ finance costs"),
]


def _warnings(f: dict[str, float]) -> list[dict]:
    """Going-concern and other warning signs in this year's figures (ISA 570 indicators)."""
    out = []
    if f.get("current_liabilities", 0) > 0 and f.get("current_assets", 0) < f["current_liabilities"]:
        out.append({"id": "net_current_liabilities", "severity": "high",
                    "text": "Current liabilities are more than current assets "
                            "(net current liabilities), a going concern indicator."})
    if f["total_equity"] < 0:
        out.append({"id": "negative_equity", "severity": "high",
                    "text": "Total equity is negative (liabilities exceed assets), "
                            "a going concern indicator."})
    if f["profit"] < 0:
        out.append({"id": "loss", "severity": "medium",
                    "text": "The company made a loss this year."})
    if f.get("finance_costs", 0) > 0 and f["operating_profit"] < 1.5 * f["finance_costs"]:
        out.append({"id": "low_interest_cover", "severity": "medium",
                    "text": "Operating profit covers finance costs less than 1.5 times."})
    if f.get("revenue", 0) > 0 and f["gross_profit"] < 0:
        out.append({"id": "negative_gross_margin", "severity": "medium",
                    "text": "Cost of sales is more than revenue (negative gross margin)."})
    return out


def compute(data: dict) -> dict:
    """Ratios for this year and, when there is a prior-year trial balance, last year."""
    has_prior = bool(data.get("has_prior"))
    cur = _figures(data, "adjusted")
    pri = _figures(data, "prior") if has_prior else None
    rows = []
    for id_, label, group, unit, formula, basis in RATIOS:
        now = formula(cur)
        before = formula(pri) if pri else None
        change = None if now is None or before is None else now - before
        flag = None
        if change is not None:
            if unit == "%":
                moved = abs(change) >= 5  # five percentage points
            else:
                moved = abs(before) > 0 and abs(change) / abs(before) >= CHANGE_THRESHOLD
            if moved:
                direction = "up" if change > 0 else "down"
                flag = (f"{direction.capitalize()} {abs(change):.1f} points on last year"
                        if unit == "%" else
                        f"{direction.capitalize()} {abs(change) / abs(before):.0%} on last year")
        rows.append({
            "id": id_, "label": label, "group": group, "group_label": GROUPS[group],
            "unit": unit, "basis": basis,
            "current": None if now is None else round(now, 2),
            "prior": None if before is None else round(before, 2),
            "change": None if change is None else round(change, 2),
            "flag": flag,
        })
    return {
        "has_prior": has_prior,
        "ratios": rows,
        "flagged": sum(1 for r in rows if r["flag"]),
        "warnings": _warnings(cur),
        "threshold": CHANGE_THRESHOLD,
    }
