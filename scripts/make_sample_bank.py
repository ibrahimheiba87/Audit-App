"""Write sample_data/sample_bank_2026.csv: the bank statement for "1000 Cash" in sample_gl.csv.

Planted for the bank reconciliation to find, at the 31 December 2026 period end:
- most ledger lines reach the bank one to three days later;
- the last receipts of December are deposits in transit, banked in January;
- the last payments of December are outstanding cheques, cleared in January;
- one August cheque never clears (an old outstanding item to follow up);
- monthly bank charges and quarterly interest that aren't in the books.
The closing balance is set so the reconciliation agrees with the trial balance (389,150.00).
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERIOD_END = date(2026, 12, 31)
BOOK_BALANCE = 389_150.00  # cash in sample_tb_2026.csv
rng = random.Random(2026)

ledger = []
with (ROOT / "sample_data" / "sample_gl.csv").open() as f:
    for r in csv.DictReader(f):
        if r["Account"] != "1000 Cash":
            continue
        amount = float(r["Debit"] or 0) - float(r["Credit"] or 0)  # money in positive
        ledger.append((date.fromisoformat(r["Date"]), r["JE Number"], r["Description"], amount))
ledger.sort()

late_receipts = [g for g in ledger if g[3] > 0 and g[0] > PERIOD_END - timedelta(days=4)][-3:]
late_payments = [g for g in ledger if g[3] < 0 and g[0] > PERIOD_END - timedelta(days=12)][-3:]
august = [g for g in ledger if g[3] < 0 and g[0].month == 8]
never = august[len(august) // 2]

lines = []  # date, description, money in (positive) or out (negative)
cheque = 4100
for g in ledger:
    day, je, text, amount = g
    if g is never:
        continue
    if g in late_receipts or g in late_payments:
        when = PERIOD_END + timedelta(days=rng.randint(2, 9))
    else:
        when = min(day + timedelta(days=rng.choice((0, 1, 1, 2, 3))), PERIOD_END)
    if amount > 0:
        lines.append((when, f"TRANSFER IN {je} {text.upper()}", amount))
    else:
        cheque += 1
        lines.append((when, f"CHQ {cheque} {text.upper()}", amount))
for month in range(1, 13):
    lines.append((date(2026, month, 28), "BANK CHARGES", -25.00))
    if month % 3 == 0:
        lines.append((date(2026, month, 28), "INTEREST CREDIT", round(rng.uniform(40, 90), 2)))
lines.append((date(2027, 1, 12), "TRANSFER IN CUSTOMER RECEIPT", 1_250.00))
lines.append((date(2027, 1, 20), "BANK CHARGES", -25.00))
lines.sort(key=lambda x: (x[0], x[1]))

# Adjusted bank = adjusted book: bank + in transit - outstanding = book + bank-only items.
dit = sum(g[3] for g in late_receipts)
outstanding = sum(g[3] for g in late_payments) + never[3]  # negative
bank_only = sum(a for _, t, a in lines if t in ("BANK CHARGES", "INTEREST CREDIT")
                and _ <= PERIOD_END)
closing = round(BOOK_BALANCE + bank_only - dit - outstanding, 2)
balance = round(closing - sum(a for d, _, a in lines if d <= PERIOD_END), 2)

with (ROOT / "sample_data" / "sample_bank_2026.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Date", "Description", "Withdrawals", "Deposits", "Balance"])
    w.writerow(["2026-01-01", "OPENING BALANCE", "", "", f"{balance:.2f}"])
    for day, text, amount in lines:
        balance = round(balance + amount, 2)
        w.writerow([day.isoformat(), text, f"{-amount:.2f}" if amount < 0 else "",
                    f"{amount:.2f}" if amount > 0 else "", f"{balance:.2f}"])
print(f"Closing balance at {PERIOD_END}: {closing:,.2f}")
