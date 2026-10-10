from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app import checks, loader

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def ledger(rows):
    df = pd.DataFrame(rows)
    df.insert(0, "row", range(2, len(df) + 2))
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    if "posted" in df:
        df["posted"] = pd.to_datetime(df["posted"])
    return df


def by_check(result, check):
    return [f for f in result["findings"] if f["check"] == check]


def test_duplicates_flag_every_copy():
    df = ledger([
        {"date": "2026-02-02", "account": "Supplies", "amount": 50.0},
        {"date": "2026-02-02", "account": "Supplies", "amount": 50.0},
        {"date": "2026-02-03", "account": "Supplies", "amount": 50.0},
    ])
    res = checks.check_duplicates(df, checks.Settings())
    assert [f["row"] for f in res.findings] == [2, 3]
    assert res.findings[0]["detail"] == "Matches row 3"


def test_round_amounts():
    df = ledger([{"amount": 5000.0}, {"amount": -12000.0}, {"amount": 5000.01}, {"amount": 900.0}])
    res = checks.check_round_amounts(df, checks.Settings())
    assert [f["row"] for f in res.findings] == [2, 3]


def test_weekend_prefers_posting_date():
    df = ledger([
        {"date": "2026-03-14", "posted": "2026-03-16", "amount": 1.0},  # Sat txn, Mon posting
        {"date": "2026-03-16", "posted": "2026-03-15", "amount": 1.0},  # Sunday posting
    ])
    res = checks.check_weekend(df, checks.Settings())
    assert [f["row"] for f in res.findings] == [3]
    assert "Sunday" in res.findings[0]["detail"]


def test_cutoff_with_explicit_period_end():
    df = ledger([
        {"date": "2026-06-10", "posted": "2026-06-10", "amount": 1.0},  # fine
        {"date": "2026-06-28", "posted": "2026-06-28", "amount": 1.0},  # near end
        {"date": "2026-06-30", "posted": "2026-07-03", "amount": 1.0},  # posted late
        {"date": "2026-07-02", "posted": "2026-07-02", "amount": 1.0},  # after end
    ])
    res = checks.check_cutoff(df, checks.Settings(period_end=date(2026, 6, 30), cutoff_days=5))
    got = {f["row"]: f["severity"] for f in res.findings}
    assert got == {3: "low", 4: "high", 5: "high"}


def test_cutoff_infers_month_end():
    df = ledger([{"date": "2026-09-12", "amount": 1.0}])
    res = checks.check_cutoff(df, checks.Settings())
    assert "2026-09-30" in res.note


def test_balance_overall_and_per_entry():
    df = ledger([
        {"entry_id": "A", "amount": 100.0}, {"entry_id": "A", "amount": -100.0},
        {"entry_id": "B", "amount": 40.0}, {"entry_id": "B", "amount": -30.0},
    ])
    res = checks.check_balance(df, checks.Settings())
    details = [f["detail"] for f in res.findings]
    assert any("differ by 10.00" in d for d in details)
    assert any("Entry B" in d for d in details)


def test_outlier_is_flagged_per_account():
    rng = np.random.default_rng(0)
    rows = [{"account": "Travel", "amount": float(a)} for a in rng.uniform(50, 500, 40)]
    rows.append({"account": "Travel", "amount": 90000.0})
    res = checks.check_outliers(ledger(rows), checks.Settings())
    assert [f["amount"] for f in res.findings] == [90000.0]


def test_benford_conforming_data():
    rng = np.random.default_rng(1)
    amounts = 10 ** rng.uniform(1, 5, 3000)
    analysis = checks.benford(ledger([{"amount": float(a)} for a in amounts]))
    assert analysis["conformity"] in {"Close conformity", "Acceptable conformity"}
    assert abs(sum(d["expected"] for d in analysis["digits"]) - 1) < 0.001


def test_benford_flags_fabricated_data():
    rng = np.random.default_rng(2)
    df = ledger([{"amount": float(a)} for a in rng.uniform(700, 999, 500)])
    result = checks.run_all(df)
    bench = next(c for c in result["checks"] if c["id"] == "benford")
    assert "Nonconformity" in bench["note"]
    assert by_check(result, "benford")


def test_benford_skips_small_samples():
    res = checks.check_benford(ledger([{"amount": 123.0}]), checks.Settings(),
                               checks.benford(ledger([{"amount": 123.0}])))
    assert res.status == "skipped"


def test_trial_balance_without_dates_skips_date_checks():
    df = ledger([{"account": "Cash", "amount": 10.0}, {"account": "Equity", "amount": -10.0}])
    result = checks.run_all(df)
    status = {c["id"]: c["status"] for c in result["checks"]}
    assert status["cutoff"] == status["weekend"] == "skipped"
    assert status["balance"] == "ran"


def test_sample_ledger_finds_planted_issues():
    df = loader.load(SAMPLE.name, SAMPLE.read_bytes())
    result = checks.run_all(df)
    descriptions = {(f["check"], f["description"]) for f in result["findings"]}
    assert ("duplicates", "Printer toner") in descriptions
    assert ("weekend", "Conference travel") in descriptions
    assert ("round_amounts", "Misc adjustment") in descriptions
    assert ("cutoff", "Year-end sale") in descriptions
    assert ("outliers", "Travel reimbursement") in descriptions
    assert ("balance", "Manual top-side entry") in descriptions
