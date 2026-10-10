"""Generate sample_data/sample_gl.csv: a balanced general ledger with planted issues."""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(42)
ACCOUNTS = {
    "1000 Cash": None, "1200 Accounts Receivable": None, "2000 Accounts Payable": None,
    "4000 Revenue": None, "5000 Cost of Sales": None, "6100 Rent": None,
    "6200 Salaries": None, "6300 Travel": None, "6400 Office Supplies": None,
}
PAIRS = [
    ("1200 Accounts Receivable", "4000 Revenue", "Invoice"),
    ("1000 Cash", "1200 Accounts Receivable", "Customer receipt"),
    ("5000 Cost of Sales", "2000 Accounts Payable", "Supplier bill"),
    ("2000 Accounts Payable", "1000 Cash", "Supplier payment"),
    ("6300 Travel", "1000 Cash", "Travel expense"),
    ("6400 Office Supplies", "1000 Cash", "Office supplies"),
]
USERS = ["asmith", "bjones", "cnguyen"]
start, end = date(2026, 1, 1), date(2026, 12, 31)
rows = []
je = 1000


def business_day():
    while True:
        d = start + timedelta(days=random.randrange((end - start).days + 1))
        if d.weekday() < 5:
            return d


def add(d, dr, cr, amount, desc, user=None, posted=None):
    global je
    je += 1
    user = user or random.choice(USERS)
    posted = posted or d
    for account, debit, credit in ((dr, amount, ""), (cr, "", amount)):
        rows.append({"JE Number": f"JE{je}", "Date": d.isoformat(), "Posted Date": posted.isoformat(),
                     "Account": account, "Description": desc, "Debit": debit, "Credit": credit,
                     "Created By": user})


for _ in range(400):
    dr, cr, desc = random.choice(PAIRS)
    if desc in ("Travel expense", "Office supplies"):
        amount = round(10 ** random.uniform(1.3, 3.3), 2)
    else:
        # Log-uniform over whole decades follows Benford's law.
        amount = round(10 ** random.uniform(1, 5), 2)
    add(business_day(), dr, cr, amount, desc)
for m in range(1, 13):
    rent_day = date(2026, m, 1)
    while rent_day.weekday() >= 5:
        rent_day += timedelta(days=1)
    add(rent_day, "6100 Rent", "1000 Cash", 4250.00, "Monthly rent")
    payday = date(2026, m, 25)
    while payday.weekday() >= 5:
        payday -= timedelta(days=1)
    add(payday, "6200 Salaries", "1000 Cash", round(random.uniform(38000, 41000), 2), "Payroll")

# Planted issues
add(date(2026, 3, 14), "6300 Travel", "1000 Cash", 2480.17, "Conference travel")  # Saturday
add(date(2026, 6, 10), "6400 Office Supplies", "1000 Cash", 389.99, "Printer toner")
add(date(2026, 6, 10), "6400 Office Supplies", "1000 Cash", 389.99, "Printer toner")  # duplicate
add(date(2026, 8, 31), "6300 Travel", "1000 Cash", 15000.00, "Misc adjustment", user="admin")  # round
add(date(2026, 12, 30), "1200 Accounts Receivable", "4000 Revenue", 48750.00, "Year-end sale",
    posted=date(2027, 1, 6))  # posted after period end
add(date(2026, 11, 20), "6300 Travel", "1000 Cash", 96500.00, "Travel reimbursement")  # outlier
rows.append({"JE Number": "JE9999", "Date": "2026-12-31", "Posted Date": "2026-12-31",
             "Account": "4000 Revenue", "Description": "Manual top-side entry", "Debit": "",
             "Credit": 12000.00, "Created By": "admin"})  # one-sided entry: unbalanced

out = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"
with out.open("w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
print(f"Wrote {len(rows)} rows to {out}")
