"""Loans and covenants, and the accounting estimates register (ISA 540)."""

import io
import re

import pandas as pd
import pytest

from app import estimates, loans
from tests.test_statements import client, engagement, map_all

LOAN = {"name": "Term loan", "lender": "Bank", "opening": 100000, "drawn": 50000,
        "repaid": 30000, "client_closing": 120000, "rate_pct": 10, "client_interest": 11000,
        "client_current": 20000, "maturity": "2030-06-30"}


def test_loans_roll_forward_and_interest_is_checked():
    eid = engagement(with_prior=False)
    map_all(eid)
    r = client.post(f"/api/engagements/{eid}/loans", json=LOAN)
    assert r.status_code == 201, r.text
    out = r.json()
    [x] = out["loans"]
    assert x["closing"] == 120000 and x["diff_closing"] == 0
    assert x["expected_interest"] == 11000 and x["diff_interest"] == 0 and x["flags"] == []
    assert out["tb"] is not None and out["tb"]["difference"] == round(120000 - out["tb"]["borrowings"], 2)

    # Interest well off, and a loan due within a year shown only partly as current.
    r = client.put(f"/api/loans/{x['id']}", json={**LOAN, "client_interest": 5000,
                                                   "maturity": "2027-06-30"})
    x = r.json()["loans"][0]
    assert set(x["flags"]) == {"interest", "current"} and x["due_within_year"]
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/loans/export").content), sheet_name=None)
    assert {"About", "Loans", "Covenants"} <= set(book)


def test_covenants_are_tested_on_the_statements():
    eid = engagement(with_prior=False)
    map_all(eid)
    loan = client.post(f"/api/engagements/{eid}/loans", json=LOAN).json()["loans"][0]
    r = client.post(f"/api/engagements/{eid}/covenants", json={
        "title": "Current ratio", "loan_id": loan["id"], "metric": "current_ratio",
        "operator": "min", "limit": 1000})
    assert r.status_code == 201, r.text
    [c] = r.json()["covenants"]
    assert c["status"] == "breached" and c["actual"] is not None and c["loan"] == "Term loan"
    assert any("IAS 1" in n for n in r.json()["notes"])
    r = client.put(f"/api/covenants/{c['id']}", json={
        "title": "DSCR", "metric": "other", "operator": "min", "limit": 1.2, "actual": 1.25})
    c = r.json()["covenants"][0]
    assert c["status"] == "close" and c["headroom"] == 0.05 and c["loan_id"] is None
    assert client.post(f"/api/engagements/{eid}/covenants", json={
        "title": "x", "metric": "nope", "limit": 1}).status_code == 400
    assert client.post(f"/api/engagements/{eid}/covenants", json={
        "title": "x", "metric": "net_worth", "limit": 1, "loan_id": 10**6}).status_code == 400
    # Deleting the loan keeps its covenants, tested on the whole company.
    client.put(f"/api/covenants/{c['id']}", json={
        "title": "Net worth", "loan_id": loan["id"], "metric": "net_worth", "limit": 1})
    out = client.delete(f"/api/loans/{loan['id']}").json()
    assert out["loans"] == [] and out["covenants"][0]["loan_id"] is None
    assert out["covenants"][0]["status"] == "met"


def test_metrics():
    f = {"current_assets": 200, "current_liabilities": 100, "debt": 50, "total_equity": 150,
         "operating_profit": 40, "finance_costs": 10, "depreciation": 10}
    assert loans.metric_value("current_ratio", f) == 2
    assert loans.metric_value("gearing", f) == 25
    assert loans.metric_value("interest_cover", f) == 4
    assert loans.metric_value("debt_to_ebitda", f) == 1
    assert loans.metric_value("debt_to_equity", {**f, "total_equity": -1}) is None
    assert loans.metric_value("current_ratio", None) is None


EST = {"title": "Warranty provision", "line": "accruals", "management": 120,
       "uncertainty": "moderate", "approach": "own_estimate", "low": 80, "high": 100,
       "prior_estimate": 90, "prior_outcome": 110}


def test_estimates_outside_the_range_and_the_look_back():
    eid = engagement(with_prior=False)
    r = client.post(f"/api/engagements/{eid}/estimates", json=EST)
    assert r.status_code == 201, r.text
    out = r.json()
    [x] = out["estimates"]
    assert x["outside"] == 20 and x["look_back"] == 20 and x["look_back_pct"] == 22.2
    assert set(x["flags"]) == {"outside", "look_back"} and out["above"] == 20
    under = client.post(f"/api/engagements/{eid}/estimates",
                        json={**EST, "title": "Under", "management": 60}).json()
    # One above our range and one below don't cancel out.
    assert under["above"] == 20 and under["below"] == -20
    gone = next(e["id"] for e in under["estimates"] if e["title"] == "Under")
    client.delete(f"/api/estimates/{gone}")
    assert client.post(f"/api/engagements/{eid}/estimates",
                       json={**EST, "low": 5, "high": 1}).status_code == 400
    assert client.post(f"/api/engagements/{eid}/estimates",
                       json={**EST, "line": "nope"}).status_code == 400
    for n in range(3):
        client.post(f"/api/engagements/{eid}/estimates",
                    json={**EST, "title": f"E{n}", "management": 99, "prior_estimate": None})
    out = client.get(f"/api/engagements/{eid}/estimates").json()
    assert any("high end" in n for n in out["notes"])
    r = client.put(f"/api/estimates/{x['id']}", json={**EST, "management": 90,
                                                     "uncertainty": "high", "assumptions": ""})
    x = next(e for e in r.json()["estimates"] if e["id"] == x["id"])
    assert x["outside"] == 0 and x["position_pct"] == 50 and "high_uncertainty" in x["flags"]
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/estimates/export").content), sheet_name=None)
    assert len(book["Estimates"]) == 4
    assert len(client.delete(f"/api/estimates/{x['id']}").json()["estimates"]) == 3


def test_roll_forward_carries_loans_and_estimates():
    eid = engagement(with_prior=False)
    loan = client.post(f"/api/engagements/{eid}/loans", json=LOAN).json()["loans"][0]
    client.post(f"/api/engagements/{eid}/loans",
                json={"name": "Old", "opening": 10, "repaid": 10, "maturity": "2026-06-30"})
    client.post(f"/api/engagements/{eid}/covenants", json={
        "title": "Gearing", "loan_id": loan["id"], "metric": "gearing", "operator": "max",
        "limit": 60})
    client.post(f"/api/engagements/{eid}/estimates", json=EST)
    new = client.post(f"/api/engagements/{eid}/roll-forward", json={}).json()
    assert new["carried"]["loans"] == 1 and new["carried"]["covenants"] == 1
    assert new["carried"]["estimates"] == 1
    out = client.get(f"/api/engagements/{new['id']}/loans").json()
    assert out["loans"][0]["opening"] == 120000 and out["loans"][0]["client_closing"] is None
    assert out["covenants"][0]["loan"] == "Term loan"
    [x] = client.get(f"/api/engagements/{new['id']}/estimates").json()["estimates"]
    assert x["prior_estimate"] == 120 and x["prior_outcome"] is None and x["low"] is None
    sections = {s["id"]: s for s in client.get(
        f"/api/engagements/{new['id']}/review").json()["sections"]}
    assert sections["estimates"]["needed"]
    acts = {a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": new["id"]}).json()}
    assert {"loan.carry", "estimate.carry"} <= acts


@pytest.mark.parametrize("module", [loans, estimates])
def test_new_text_has_arabic(module):
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    source = open(module.__file__, encoding="utf-8").read()
    texts = re.findall(r'HTTPException\(\d+, f?"([^"]+)"', source)
    texts = [re.sub(r"\{[^}]+\}", "9", t) for t in texts if not t.endswith(" ")]
    for name in ("METRICS", "OPERATORS", "STATUS_LABELS", "UNCERTAINTY", "APPROACHES"):
        for v in getattr(module, name, {}).values():
            texts.append(v[0] if isinstance(v, tuple) else v)
    missing = sorted({t for t in texts if t and not translated(t)})
    assert not missing, missing


def test_a_loan_due_soon_with_nothing_shown_as_current_is_flagged():
    eid = engagement(with_prior=False)
    out = client.post(f"/api/engagements/{eid}/loans", json={
        "name": "Bridge", "opening": 50000, "maturity": "2027-02-28"}).json()
    assert out["loans"][0]["flags"] == ["current"]
