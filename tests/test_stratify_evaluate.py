"""Stratification and summaries, and evaluating a tested sample."""

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import sampling, stratify
from app.db import Activity, SessionLocal
from app.main import app
from tests.test_sampling import P, ledger, setup_engagement

client = TestClient(app)


# --- Stratification ----------------------------------------------------------


def test_strata_cover_every_line_once():
    df = ledger([50.0, -150.0, 999.99, 1000.0, -25_000.0, 0.0])
    df["user"] = ["ann", "bob", "ann", None, "ann", "bob"]
    df["date"] = pd.to_datetime(["2026-01-05", "2026-01-20", "2026-02-01", "2026-02-02",
                                 "2026-03-31", None])
    r = stratify.analyze(df, [0, 100, 1000, 10_000])
    assert [s["lines"] for s in r["strata"]] == [2, 2, 1, 1]
    assert sum(s["lines"] for s in r["strata"]) == len(df)
    assert r["strata"][2] == {"from": 1000.0, "to": 10_000.0, "lines": 1, "debits": 1000.0,
                              "credits": 0.0, "value": 1000.0, "lines_pct": 16.7,
                              "value_pct": round(100 * 1000 / 27_199.99, 1)}
    assert r["strata"][-1]["to"] is None and r["strata"][-1]["credits"] == 25_000.0
    users = {row["key"]: row for row in r["summaries"]["user"]["rows"]}
    assert users["ann"]["lines"] == 3 and users["ann"]["largest"] == -25_000.0
    assert users["(blank)"]["lines"] == 1
    months = [row["key"] for row in r["summaries"]["month"]["rows"]]
    assert months == ["(no date)", "2026-01", "2026-02", "2026-03"]
    amount = next(f for f in r["fields"] if f["field"] == "amount")
    assert amount["zeros"] == 1 and amount["negatives"] == 2 and amount["min"] == -25_000.0
    user = next(f for f in r["fields"] if f["field"] == "user")
    assert (user["blank"], user["distinct"], user["most_common"]) == (1, 2, "ann")


def test_default_bands_use_materiality_or_powers_of_ten():
    assert stratify.default_bands(5_000) == [0.0, 100.0, 1000.0]
    m = {"trivial_threshold": 2_250, "performance_materiality": 45_000,
         "overall_materiality": 60_000}
    assert stratify.default_bands(5_000, m) == [0.0, 2250.0, 45_000.0, 60_000.0]
    assert stratify.check_bands([1000, 10]) == [0.0, 10.0, 1000.0]
    for bad in ([], [-1], [float("nan")], list(range(30))):
        with pytest.raises(stratify.StratifyError):
            stratify.check_bands(bad)


def test_summaries_skip_missing_columns_and_excel_has_every_sheet():
    r = stratify.analyze(ledger([10.0, 20.0]))
    assert set(r["summaries"]) == {"account"}
    sheets = pd.read_excel(io.BytesIO(stratify.to_excel(r, {"Client": "=cmd"})), sheet_name=None)
    assert set(sheets) == {"About", "Stratification", "By account", "Field statistics"}
    assert "=cmd" in sheets["About"]["Value"].tolist()  # stored as text, not a formula


def test_stratify_api_logs_and_exports():
    eid, uid = setup_engagement()
    r = client.post(f"/api/uploads/{uid}/stratify", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bands"] == [0.0, 3000.0, 45_000.0, 60_000.0]  # the engagement's materiality
    assert sum(s["lines"] for s in body["strata"]) == body["totals"]["lines"]
    custom = client.post(f"/api/uploads/{uid}/stratify", json={"bands": [0, 500]}).json()
    assert custom["bands"] == [0.0, 500.0]
    assert client.post(f"/api/uploads/{uid}/stratify", json={"bands": [-5]}).status_code == 400
    xlsx = client.get(f"/api/uploads/{uid}/stratify/export?bands=0,500")
    assert xlsx.status_code == 200
    strata = pd.read_excel(io.BytesIO(xlsx.content), sheet_name="Stratification")
    assert len(strata) == 2
    assert client.get(f"/api/uploads/{uid}/stratify/export?bands=x").status_code == 400
    with SessionLocal() as s:
        actions = [a.action for a in s.query(Activity).filter(Activity.engagement_id == eid)]
    assert actions.count("analysis.stratify") == 2 and "analysis.download" in actions


def test_summary_rows_are_trimmed_for_the_page():
    df = ledger([float(i) for i in range(1, 601)])
    df["account"] = [f"A{i}" for i in range(600)]
    out = stratify.trimmed(stratify.analyze(df))
    acc = out["summaries"]["account"]
    assert len(acc["rows"]) == stratify.MAX_ROWS and acc["truncated"] == 100


# --- Sample evaluation -------------------------------------------------------


def test_poisson_upper_limits_match_the_published_tables():
    assert [sampling.poisson_upper_limit(k, 95) for k in range(4)] == pytest.approx(
        [3.00, 4.75, 6.30, 7.76], abs=0.01)
    assert sampling.poisson_upper_limit(0, 90) == pytest.approx(2.31, abs=0.01)
    assert sampling.poisson_upper_limit(1, 80) == pytest.approx(2.99, abs=0.01)


def at_book(result, change=None):
    out = [{"row": i["row"], "audited_amount": abs(i["amount"]), "notes": ""}
           for i in result["items"]]
    for row, audited in (change or {}).items():
        next(t for t in out if t["row"] == row)["audited_amount"] = audited
    return out


def test_mus_evaluation_projects_taintings_and_top_stratum():
    df = ledger([100.0] * 99 + [50_000.0])
    r = sampling.draw(df, P("mus", sample_size=10, seed=3))
    interval = r["details"]["interval"]
    small = next(i for i in r["items"] if abs(i["amount"]) < interval)
    big = next(i for i in r["items"] if abs(i["amount"]) >= interval)
    clean = sampling.evaluate(r, at_book(r), 1e9)
    assert clean["exceptions"] == 0 and clean["projected_misstatement"] == 0
    assert clean["upper_limit"] == pytest.approx(3.0 * interval, rel=0.01)
    ev = sampling.evaluate(r, at_book(r, {small["row"]: 60.0, big["row"]: 49_000.0}), 1e9)
    assert ev["exceptions"] == 2 and ev["known_misstatement"] == 1040.0
    by_row = {i["row"]: i for i in ev["items"]}
    assert by_row[small["row"]]["tainting"] == 0.4
    assert by_row[small["row"]]["projected"] == pytest.approx(0.4 * interval, abs=0.01)
    assert by_row[big["row"]]["projected"] == 1000.0  # top stratum: not projected
    expected = (sampling.poisson_upper_limit(0, 95) * interval
                + (sampling.poisson_upper_limit(1, 95) - sampling.poisson_upper_limit(0, 95))
                * 0.4 * interval + 1000.0)
    assert ev["upper_limit"] == pytest.approx(expected, abs=0.05)
    assert ev["conclusion"]["status"] == "acceptable"
    tight = sampling.evaluate(r, at_book(r), interval)  # basic precision alone is 3 intervals
    assert tight["conclusion"]["status"] == "exceeds"


def test_ratio_evaluation_and_conclusions():
    df = ledger([1000.0] * 100)
    r = sampling.draw(df, P("random", sample_size=10, seed=1))
    rows = [i["row"] for i in r["items"]]
    ev = sampling.evaluate(r, at_book(r, {rows[0]: 900.0}), 50_000)
    assert ev["basis"] == "ratio" and ev["upper_limit"] is None
    assert ev["projected_misstatement"] == 1000.0  # 100 of 10,000 tested, over 100,000
    assert ev["exception_rate"] == 10.0 and ev["conclusion"]["status"] == "acceptable"
    assert sampling.evaluate(r, at_book(r, {rows[0]: 900.0}), 1500)["conclusion"]["status"] \
        == "close"
    assert sampling.evaluate(r, at_book(r, {rows[0]: 900.0}), 900)["conclusion"]["status"] \
        == "exceeds"
    assert sampling.evaluate(r, at_book(r), None)["conclusion"]["status"] == "no_tolerable"
    part = sampling.evaluate(r, at_book(r)[:4], 50_000)
    assert part["untested"] == 6 and part["conclusion"]["status"] == "incomplete"
    with pytest.raises(sampling.SamplingError, match="not in this sample"):
        sampling.evaluate(r, [{"row": 99_999, "audited_amount": 1.0}], 1.0)


def test_evaluation_api_saves_logs_and_exports():
    eid, uid = setup_engagement()
    s = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": uid, "method": "random", "sample_size": 5,
                          "seed": 7}).json()
    items = s["result"]["items"]
    got = client.get(f"/api/samples/{s['id']}").json()
    assert got["evaluation"] is None and got["default_tolerable"] == 45_000
    body = {"items": [{"row": i["row"], "audited_amount": abs(i["amount"]), "notes": "ok"}
                      for i in items]}
    body["items"][0]["audited_amount"] = 0
    body["items"][0]["notes"] = "=no invoice"
    r = client.put(f"/api/samples/{s['id']}/evaluation", json=body)
    assert r.status_code == 200, r.text
    ev = r.json()["evaluation"]
    assert ev["tested"] == 5 and ev["exceptions"] == 1
    assert ev["tolerable_misstatement"] == 45_000 and ev["tolerable_entered"] is None
    listed = client.get(f"/api/engagements/{eid}").json()["samples"][0]
    assert listed["evaluation"]["status"] in sampling.CONCLUSIONS
    xlsx = client.get(f"/api/samples/{s['id']}/export")
    sheets = pd.read_excel(io.BytesIO(xlsx.content), sheet_name=None)
    assert "Evaluation" in sheets
    assert "=no invoice" in sheets["Sample"]["Notes"].tolist()
    # Saving again replaces the results; a row from outside the sample is refused.
    body["tolerable_misstatement"] = 1
    again = client.put(f"/api/samples/{s['id']}/evaluation", json=body).json()["evaluation"]
    assert again["tolerable_entered"] == 1 and again["conclusion"]["status"] == "exceeds"
    bad = {"items": [{"row": 10**9, "audited_amount": 1}]}
    assert client.put(f"/api/samples/{s['id']}/evaluation", json=bad).status_code == 400
    dup = {"items": [body["items"][0], body["items"][0]]}
    assert client.put(f"/api/samples/{s['id']}/evaluation", json=dup).status_code == 400
    neg = {"items": [{"row": items[0]["row"], "audited_amount": -1}]}
    assert client.put(f"/api/samples/{s['id']}/evaluation", json=neg).status_code == 422
    with SessionLocal() as session:
        actions = [a.action for a in session.query(Activity)]
    assert actions.count("sample.evaluate") == 2
    # Recorded results are audit evidence, so their file can't be deleted.
    r = client.delete(f"/api/uploads/{uid}")
    assert r.status_code == 409 and "test results" in r.json()["detail"]
    assert client.get(f"/api/samples/{s['id']}").status_code == 200


def test_offsetting_errors_do_not_cancel_out():
    df = ledger([1000.0, -1000.0, 500.0])
    r = sampling.draw(df, P("random", sample_size=3, seed=1))
    rows = {abs(i["amount"]) * (1 if i["amount"] > 0 else -1): i["row"] for i in r["items"]}
    ev = sampling.evaluate(r, at_book(r, {rows[1000.0]: 0.0, rows[-1000.0]: 2000.0}), 1500)
    assert ev["known_misstatement"] == 0 and ev["projected_misstatement"] == 0
    assert ev["projected_overstatement"] == 1000.0 and ev["projected_understatement"] == 1000.0
    assert ev["conclusion"]["status"] == "close"
    assert sampling.evaluate(r, at_book(r, {rows[1000.0]: 0.0, rows[-1000.0]: 2000.0}),
                             900)["conclusion"]["status"] == "exceeds"
