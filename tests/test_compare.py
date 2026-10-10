import io
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import compare, loader
from app.main import app

client = TestClient(app)
DATA = Path(__file__).resolve().parent.parent / "sample_data"
TB26 = DATA / "sample_tb_2026.csv"
TB25 = DATA / "sample_tb_2025.csv"


def tb(rows):
    df = pd.DataFrame(rows, columns=["account", "description", "amount"])
    df.insert(0, "row", range(2, len(df) + 2))
    return df


def by_account(result):
    return {r["account"]: r for r in result["rows"]}


def test_flags_large_changes_and_new_and_removed_accounts():
    current = tb([("1000", "Cash", 100.0), ("4000", "Revenue", -1500.0), ("6500", "Fees", 300.0)])
    prior = tb([("1000", "Cash", 95.0), ("4000", "Revenue", -1000.0), ("6600", "Ads", 200.0)])
    r = by_account(compare.compare(current, prior, compare.CompareSettings(150, 10)))
    assert not r["1000"]["flagged"]
    assert r["4000"]["change"] == -500.0
    assert r["4000"]["change_pct"] == 50.0  # credit balance grew by half
    assert r["4000"]["flagged"]
    assert "New account this year" in r["6500"]["reasons"]
    assert "Account not in this year's file" in r["6600"]["reasons"]
    assert r["6600"]["current"] == 0.0


def test_percentage_threshold_suppresses_small_relative_changes():
    current = tb([("1500", "PPE", 1_100_000.0)])
    prior = tb([("1500", "PPE", 1_000_000.0)])
    r = compare.compare(current, prior, compare.CompareSettings(50_000, 15))
    assert not r["rows"][0]["flagged"]  # +100k is only +10%
    r = compare.compare(current, prior, compare.CompareSettings(50_000, 5))
    assert r["rows"][0]["flagged"]


def test_sign_flip_is_reported():
    r = compare.compare(tb([("1000", "Bank", -60_000.0)]), tb([("1000", "Bank", 40_000.0)]),
                        compare.CompareSettings(50_000, 10))
    assert "Balance switched between debit and credit" in r["rows"][0]["reasons"]


def test_general_ledger_lines_are_summed_per_account():
    current = tb([("6300", "Taxi", 100.0), ("6300", "Hotel", 400.0)])
    prior = tb([("6300", "Taxi", 450.0)])
    r = compare.compare(current, prior, compare.CompareSettings(10, 0))
    assert r["rows"][0]["current"] == 500.0
    assert r["rows"][0]["name"] == "Taxi"


def test_needs_account_column_and_positive_threshold():
    no_account = pd.DataFrame({"row": [2], "amount": [1.0]})
    with pytest.raises(compare.CompareError, match="account column"):
        compare.compare(no_account, no_account, compare.CompareSettings(1, 0))
    with pytest.raises(compare.CompareError, match="more than zero"):
        compare.compare(tb([]), tb([]), compare.CompareSettings(0, 0))


def test_sample_trial_balances():
    cur = loader.load(TB26.name, TB26.read_bytes()).df
    pri = loader.load(TB25.name, TB25.read_bytes()).df
    result = compare.compare(cur, pri, compare.CompareSettings(45_000, 10))
    r = by_account(result)
    assert result["summary"]["current_total"] == 0 and result["summary"]["prior_total"] == 0
    assert r["4000"]["name"] == "Revenue" and r["4000"]["change_pct"] == 25.0
    assert r["6300"]["flagged"] and r["6500"]["flagged"] and r["6600"]["flagged"]
    assert not r["6100"]["flagged"]


def test_stateless_compare_endpoint_and_excel():
    files = {"current": (TB26.name, TB26.read_bytes(), "text/csv"),
             "prior": (TB25.name, TB25.read_bytes(), "text/csv")}
    r = client.post("/api/compare", files=files, data={"threshold_amount": "45000"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["flagged"] == 7
    r = client.post("/api/compare", files=files,
                    data={"threshold_amount": "45000", "format": "xlsx"})
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert set(sheets) == {"Summary", "Comparison"}
    assert len(sheets["Comparison"]) == 17


def setup_engagement(with_materiality=True):
    c = client.post("/api/clients", json={"name": "Acme"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    if with_materiality:
        client.put(f"/api/engagements/{e['id']}/materiality",
                   json={"benchmark": "revenue", "benchmark_amount": 6_000_000})
    ups = {}
    for path, kind in ((TB26, "tb"), (TB25, "prior_tb")):
        r = client.post(f"/api/engagements/{e['id']}/uploads", data={"kind": kind},
                        files={"file": (path.name, path.read_bytes(), "text/csv")})
        ups[kind] = r.json()["id"]
    return e["id"], ups


def test_engagement_comparison_uses_performance_materiality():
    eid, ups = setup_engagement()
    r = client.post(f"/api/engagements/{eid}/comparisons",
                    json={"current_upload_id": ups["tb"], "prior_upload_id": ups["prior_tb"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["settings"]["threshold_amount"] == 45_000
    assert body["summary"]["flagged"] == 7
    detail = client.get(f"/api/engagements/{eid}").json()
    assert [c["id"] for c in detail["comparisons"]] == [body["id"]]
    report = client.get(f"/api/comparisons/{body['id']}/report")
    summary = pd.read_excel(io.BytesIO(report.content), sheet_name="Summary", header=None)
    assert "Acme" in summary[1].tolist()


def test_engagement_comparison_needs_threshold_without_materiality():
    eid, ups = setup_engagement(with_materiality=False)
    body = {"current_upload_id": ups["tb"], "prior_upload_id": ups["prior_tb"]}
    assert client.post(f"/api/engagements/{eid}/comparisons", json=body).status_code == 400
    r = client.post(f"/api/engagements/{eid}/comparisons", json={**body, "threshold_amount": 200_000})
    assert r.status_code == 201
    assert r.json()["summary"]["flagged"] == 5  # 3 over 200k plus the new and removed accounts


def test_engagement_comparison_rejects_same_file_and_deletes_with_upload():
    eid, ups = setup_engagement()
    same = {"current_upload_id": ups["tb"], "prior_upload_id": ups["tb"]}
    assert client.post(f"/api/engagements/{eid}/comparisons", json=same).status_code == 400
    client.post(f"/api/engagements/{eid}/comparisons",
                json={"current_upload_id": ups["tb"], "prior_upload_id": ups["prior_tb"]})
    client.delete(f"/api/uploads/{ups['prior_tb']}")
    assert client.get(f"/api/engagements/{eid}").json()["comparisons"] == []
