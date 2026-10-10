"""Write sample_data/sample_tb_2025.csv and sample_tb_2026.csv for the prior-year comparison.

Planted for the comparison to find: revenue and receivables up sharply, travel up more than
4x, marketing gone and a new consulting fees account this year.
"""

import csv
from pathlib import Path

# account, name, 2025 balance, 2026 balance (debit positive, credit negative)
ACCOUNTS = [
    ("1000", "Cash", 412_300.00, 389_150.00),
    ("1200", "Accounts Receivable", 615_400.00, 948_200.00),
    ("1300", "Inventory", 820_000.00, 845_500.00),
    ("1500", "Property and Equipment", 1_240_000.00, 1_198_000.00),
    ("2000", "Accounts Payable", -498_700.00, -512_900.00),
    ("2100", "Accrued Liabilities", -142_000.00, -151_300.00),
    ("2500", "Bank Loan", -900_000.00, -750_000.00),
    ("3000", "Share Capital", -500_000.00, -500_000.00),
    ("3100", "Retained Earnings", -422_500.00, -405_810.00),
    ("4000", "Revenue", -5_480_000.00, -6_850_000.00),
    ("5000", "Cost of Sales", 3_290_000.00, 4_005_000.00),
    ("6100", "Rent", 51_000.00, 51_000.00),
    ("6200", "Salaries", 1_384_500.00, 1_428_900.00),
    ("6300", "Travel", 38_200.00, 176_430.00),
    ("6400", "Office Supplies", 21_800.00, 23_480.00),
    ("6500", "Consulting Fees", None, 104_350.00),
    ("6600", "Marketing", 70_000.00, None),
]

out_dir = Path(__file__).resolve().parent.parent / "sample_data"
for year, col in ((2025, 2), (2026, 3)):
    rows = [a for a in ACCOUNTS if a[col] is not None]
    assert abs(sum(a[col] for a in rows)) < 0.005, f"{year} TB does not balance"
    with (out_dir / f"sample_tb_{year}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Account", "Account Name", "Debit", "Credit"])
        for acct, name, *bal in rows:
            amount = bal[col - 2]
            w.writerow([acct, name, f"{amount:.2f}" if amount > 0 else "",
                        f"{-amount:.2f}" if amount < 0 else ""])
    print(f"Wrote sample_tb_{year}.csv with {len(rows)} accounts")
