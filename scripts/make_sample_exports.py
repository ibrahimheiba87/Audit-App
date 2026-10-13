"""Write sample_data/nile_trading_*.xlsx: a ledger and trial balances laid out like an
accounting system's export, with title rows above the header, dd/mm/yyyy dates, amounts as
text with thousands separators and a total row.

The ledger has planted issues: a round Friday payment by the finance director, a year-end
accrual entered after period end, a duplicate travel claim and an entry that doesn't balance.
"""

import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

random.seed(7)
OUT = Path(__file__).resolve().parent.parent / "sample_data"
ACCOUNTS = {
    "1010": "Cash at Bank - CIB", "1100": "Trade Receivables", "1300": "Inventory",
    "1500": "Plant and Equipment", "2010": "Trade Payables", "2200": "VAT Payable",
    "2500": "Bank Loan", "3000": "Share Capital", "3100": "Retained Earnings",
    "4010": "Sales - Local", "4020": "Sales - Export", "5010": "Cost of Goods Sold",
    "6100": "Office Rent", "6200": "Salaries and Wages", "6300": "Travel and Accommodation",
    "6400": "Office Supplies", "6500": "Professional Fees", "6600": "Bank Charges",
}
STAFF = ["m.hassan", "s.adel", "o.farouk"]
OPENING = {  # last year's closing balances, debit positive
    "1010": 1_250_000.0, "1100": 2_340_000.0, "1300": 1_910_000.0, "1500": 4_600_000.0,
    "2010": -1_480_000.0, "2200": -210_000.0, "2500": -3_000_000.0, "3000": -2_000_000.0,
    "3100": -3_410_000.0,
}
PRIOR_PL = {"4010": -11_200_000.0, "4020": -2_900_000.0, "5010": 9_100_000.0, "6100": 960_000.0,
            "6200": 4_620_000.0, "6300": 61_000.0, "6400": 38_000.0, "6500": 120_000.0,
            "6600": 21_000.0}
lines = []
movements = dict.fromkeys(ACCOUNTS, 0.0)
next_journal = 5000


def business_day():
    start, days = date(2026, 1, 1), 365
    while True:
        d = start + timedelta(days=random.randrange(days))
        if d.weekday() not in (4, 5):  # Friday and Saturday are the weekend
            return d


def post(d, legs, text, user=None, entered=None):
    global next_journal
    next_journal += 1
    user = user or random.choice(STAFF)
    entered = entered or d + timedelta(days=random.choice([0, 0, 0, 1, 2]))
    for account, amount in legs:
        movements[account] += amount
        lines.append((d, f"JV-{next_journal}", account, text, amount, user, entered))


for _ in range(1100):
    kind, d = random.random(), business_day()
    amount = round(10 ** random.uniform(3, 5), 2)
    if kind < .35:
        amount = round(10 ** random.uniform(3, 5.3), 2)
        if random.random() < 2 / 3:
            vat = round(amount * .14, 2)
            legs = [("1100", amount + vat), ("4010", -amount), ("2200", -vat)]
        else:
            legs = [("1100", amount), ("4020", -amount)]
        post(d, legs, f"Sales invoice INV-{random.randint(10000, 99999)}")
    elif kind < .6:
        post(d, [("1010", amount * 1.6), ("1100", -amount * 1.6)], "Customer receipt")
    elif kind < .8:
        post(d, [("5010", amount), ("2010", -amount)], f"Supplier bill {random.randint(100, 999)}")
    elif kind < .93:
        amount = round(10 ** random.uniform(3, 4.8), 2)
        post(d, [("2010", amount), ("1010", -amount)], "Supplier payment")
    else:
        account, text = random.choice([("6300", "Travel claim"), ("6400", "Stationery"),
                                       ("6600", "Bank charges"), ("6500", "Legal advice")])
        amount = round(10 ** random.uniform(1.5, 3.8), 2)
        post(d, [(account, amount), ("1010", -amount)], text)
for month in range(1, 13):
    post(date(2026, month, 1), [("6100", 85_000.0), ("1010", -85_000.0)], "Monthly office rent")
    post(date(2026, month, 25), [("6200", 412_500.0), ("1010", -412_500.0)], "Payroll")

post(date(2026, 3, 13), [("6500", 250_000.0), ("1010", -250_000.0)], "Consultancy - urgent",
     user="finance.director")
post(date(2026, 12, 31), [("1100", 1_800_000.0), ("4020", -1_800_000.0)],
     "Year end sales accrual", user="finance.director", entered=date(2027, 1, 18))
for _ in range(2):
    post(date(2026, 7, 4), [("6300", 4_999.0), ("1010", -4_999.0)], "Travel claim")
post(date(2026, 9, 9), [("6400", 1_200.0), ("1010", -1_000.0)], "Stationery")


def money(x):
    return f"{x:,.2f}" if x else ""


def write(path, titles, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    for title in titles:
        ws.append([title])
    ws.append([])
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


rows = [(d.strftime("%d/%m/%Y"), journal, account, ACCOUNTS[account], text,
         money(max(amount, 0)), money(max(-amount, 0)), user, entered.strftime("%d/%m/%Y"))
        for d, journal, account, text, amount, user, entered in sorted(lines)]
debits = sum(line[4] for line in lines if line[4] > 0)
credits = -sum(line[4] for line in lines if line[4] < 0)
rows.append(("", "", "", "", "Total", money(debits), money(credits), "", ""))
write(OUT / "nile_trading_gl_2026.xlsx",
      ["Nile Trading S.A.E.", "General Ledger Detail", "Period: 01/01/2026 - 31/12/2026"],
      ["Date", "Journal No.", "Account Code", "Account Name", "Narration", "Debit (EGP)",
       "Credit (EGP)", "Entered By", "Entered On"], rows)


def trial_balance(year, balances):
    # The unbalanced entry and the unclosed profit go to retained earnings, so the TB balances.
    balances = {**balances, "3100": balances["3100"] - round(sum(balances.values()), 2)}
    rows = [(a, ACCOUNTS[a], round(max(v, 0), 2) or None, round(max(-v, 0), 2) or None)
            for a, v in sorted(balances.items()) if round(v, 2)]
    rows.append(("", "Total", round(sum(r[2] or 0 for r in rows), 2),
                 round(sum(r[3] or 0 for r in rows), 2)))
    write(OUT / f"nile_trading_tb_{year}.xlsx",
          ["Nile Trading S.A.E.", "Trial Balance", f"As at 31 December {year}"],
          ["Account Code", "Account Name", "Debit", "Credit"], rows)


trial_balance(2026, {a: OPENING.get(a, 0) + movements[a] for a in ACCOUNTS})
trial_balance(2025, {**OPENING, **PRIOR_PL})
print(f"Wrote {len(rows)} ledger rows and two trial balances to {OUT}")
