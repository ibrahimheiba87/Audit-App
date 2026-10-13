"""Older review findings (PRs #1-#22) that were still open, each pinned by a test."""

import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import checks, compare, loader, materiality, statements
from app.main import app

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def ledger(rows):
    df = pd.DataFrame(rows)
    df.insert(0, "row", range(2, len(df) + 2))
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    return df


def test_repeated_trial_balance_amounts_are_not_duplicates():
    tb = ledger([{"account": "1000", "amount": 50.0}, {"account": "2000", "amount": 50.0},
                 {"account": "1000", "amount": 50.0}])
    res = checks.check_duplicates(tb, checks.Settings())
    assert res.status == "skipped" and not res.findings


def test_round_amounts_survive_float_remainders():
    df = ledger([{"amount": 3.3}, {"amount": 2.2}, {"amount": 3.4}])
    res = checks.check_round_amounts(df, checks.Settings(round_threshold=1.1))
    assert sorted(f["amount"] for f in res.findings) == [2.2, 3.3]


def test_quick_audit_rejects_nonsense_settings():
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    for bad in ({"cutoff_days": "-1"}, {"round_threshold": "0"}, {"round_threshold": "inf"},
                {"performance_materiality": "-5"}, {"performance_materiality": "nan"}):
        for url in ("/api/audit", "/api/report"):
            r = client.post(url, files=files, data=bad)
            assert r.status_code == 400, (url, bad, r.text)


def test_duplicate_headers_after_trimming_are_reported():
    with pytest.raises(loader.LoadError, match="more than one column named 'Amount'"):
        loader.load("x.csv", b"Date,Amount, Amount \n2026-01-01,1,2\n")


def test_materiality_rejects_non_finite_amounts():
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            materiality.calculate("revenue", bad)


def test_comparison_threshold_uses_the_unrounded_change():
    cur = pd.DataFrame({"account": ["1000"], "amount": [109.96]})
    pri = pd.DataFrame({"account": ["1000"], "amount": [100.0]})
    out = compare.compare(cur, pri, compare.CompareSettings(threshold_amount=1, threshold_pct=10))
    row = out["accounts"][0] if "accounts" in out else out["rows"][0]
    assert row["change_pct"] == 10.0 and not row["flagged"]  # 9.96%: shown rounded, not flagged


def test_accounts_only_on_proposed_entries_can_be_mapped():
    tb = pd.DataFrame({"account": ["1000", "4000"], "description": ["Cash", "Sales"],
                       "amount": [1000.0, -1000.0]})
    proposed = {"id": 1, "kind": "proposed", "ref": "PAJE-1", "description": "x",
                "lines": [{"account": "9999", "debit": 50, "credit": 0},
                          {"account": "1000", "debit": 0, "credit": 50}]}
    out = statements.build(tb, None, {"1000": "cash", "4000": "revenue"}, [proposed])
    rows = {r["account"]: r for r in out["accounts"]}
    assert rows["9999"]["proposed_only"] and rows["9999"]["adjusted"] == 0
    assert not rows["1000"]["proposed_only"] and rows["1000"]["adjusted"] == 1000  # not booked


def test_export_shows_a_prior_year_difference():
    tb = pd.DataFrame({"account": ["1000", "3000"], "amount": [100.0, -100.0]})
    prior = pd.DataFrame({"account": ["1000", "3000"], "amount": [100.0, -90.0]})
    out = statements.build(tb, prior, {"1000": "cash", "3000": "share_capital"}, [])
    assert out["statements"]["difference"] == {"current": 0, "prior": 10}
    book = pd.read_excel(io.BytesIO(statements.to_excel(out, [])), sheet_name=None, header=None)
    cells = [c for sheet in book.values() for c in sheet.values.ravel().tolist()]
    assert "Balance sheet difference (prior year)" in cells


def test_run_report_keeps_the_names_from_when_it_ran():
    c = client.post("/api/clients", json={"name": "Acme Ltd"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026 audit", "period_end": "2026-12-31"}).json()
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    up = client.post(f"/api/engagements/{e['id']}/uploads", files=files, data={}).json()
    run = client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]}).json()
    assert client.patch(f"/api/engagements/{e['id']}", json={"name": "Renamed"}).status_code == 200
    r = client.get(f"/api/runs/{run['id']}/report")
    summary = pd.read_excel(io.BytesIO(r.content), sheet_name="Summary", header=None)
    assert "FY2026 audit" in summary[1].tolist() and "Renamed" not in summary[1].tolist()


def test_setup_code_is_hidden_as_it_is_typed():
    assert 'id="setup-code" name="setup_code" type="password"' in client.get("/").text
