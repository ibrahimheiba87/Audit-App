"""Analytical procedures and proofs in total: expectations built from inputs and compared
with the lead sheets."""

import io

import pandas as pd
import pytest

from tests.test_related_summaries import steps
from tests.test_review import section, team

pytestmark = pytest.mark.usefixtures("real_auth")
TB = """account,description,amount
1000,Cash,50000
1500,Equipment,120000
2500,Bank loan,-200000
3000,Share capital,-20000
4000,Sales,-1100000
6100,Salaries,600000
6800,Depreciation,12000
7000,Interest,18500
8000,Other expenses,519500
"""
PRIOR = """account,description,amount
1000,Cash,40000
4000,Sales,-1000000
6100,Salaries,560000
"""
MAPPING = {"1000": "cash", "1500": "ppe", "2500": "borrowings", "3000": "share_capital",
           "4000": "revenue", "6100": "staff_costs", "6800": "depreciation",
           "7000": "finance_costs", "8000": "other_expenses"}


def books(c, eid):
    for kind, text in (("tb", TB), ("prior_tb", PRIOR)):
        r = c.post(f"/api/engagements/{eid}/uploads", data={"kind": kind},
                   files={"file": (f"{kind}.csv", text.encode(), "text/csv")})
        assert r.status_code == 201, r.text
    assert c.put(f"/api/engagements/{eid}/mapping",
                 json={"mappings": MAPPING}).status_code == 200


def test_an_expectation_from_last_year_needs_an_explanation_then_a_review():
    a, prep, rev, partner, eid, _ = team()
    books(prep, eid)
    url = f"/api/engagements/{eid}/expectations"
    r = prep.post(url, json={"kind": "analytic", "line": "revenue",
                             "inputs": {"change_pct": 5},
                             "basis": "Price list up 5%, volumes flat per the sales report"})
    assert r.status_code == 201, r.text
    x = r.json()["expectations"][0]
    # Last year's 1,000,000 up 5% is 1,050,000; the books show 1,100,000. No materiality yet.
    assert x["expected"] == 1_050_000 and x["actual"] == 1_100_000 and x["difference"] == 50_000
    assert x["outcome"] == "no_threshold"
    xid = x["id"]
    x = prep.patch(f"/api/expectations/{xid}", json={"threshold": 30_000}).json()[
        "expectations"][0]
    assert x["outcome"] == "to_explain" and x["difference_pct"] == 4.8
    assert any("analytical procedures" in s for s in steps(prep, eid))
    r = rev.patch(f"/api/expectations/{xid}", json={"reviewed": True})
    assert r.status_code == 400 and "Difference to explain" in r.json()["detail"]
    prep.patch(f"/api/expectations/{xid}",
               json={"explanation": "A new customer from October; agreed to its contract.",
                     "conclusion": "Revenue is reasonable."})
    assert prep.patch(f"/api/expectations/{xid}", json={"reviewed": True}).status_code == 403
    x = rev.patch(f"/api/expectations/{xid}", json={"reviewed": True}).json()["expectations"][0]
    assert x["outcome"] == "explained" and x["reviewed_by"] == "Reviewer"
    assert not x["review_stale"]

    # An adjusting entry moves the actual, so the review no longer stands.
    r = rev.post(f"/api/engagements/{eid}/adjusting-entries", json={
        "kind": "adjusting", "description": "Cut-off",
        "lines": [{"account": "4000", "debit": 10_000}, {"account": "1000", "credit": 10_000}]})
    assert r.status_code in (200, 201), r.text
    x = prep.get(url).json()["expectations"][0]
    assert x["actual"] == 1_090_000 and x["review_stale"]
    assert section(prep, eid, "analytics")["needed"]


def test_proofs_in_total_for_depreciation_payroll_and_interest():
    a, prep, rev, partner, eid, _ = team()
    books(prep, eid)
    url = f"/api/engagements/{eid}/expectations"
    prep.post(url, json={"kind": "depreciation",
                         "inputs": {"base": 120_000, "rate_pct": 10}, "threshold": 1000})
    prep.post(url, json={"kind": "payroll",
                         "inputs": {"units": 10, "price": 5000}, "threshold": 1000})
    r = prep.post(url, json={"kind": "interest", "inputs": {"base": 200_000, "rate_pct": 9,
                                                             "months": 10}, "threshold": 500})
    dep, pay, intr = r.json()["expectations"]
    assert (dep["line"], dep["expected"], dep["actual"], dep["outcome"]) == \
        ("depreciation", 12_000, 12_000, "within")
    assert pay["inputs"]["periods"] == 12 and pay["expected"] == 600_000
    assert pay["outcome"] == "within" and pay["title"] == "Payroll proof"
    assert intr["expected"] == 15_000 and intr["difference"] == 3_500
    # Finance costs hold bank charges too: the interest itself is entered, with its source.
    r = prep.patch(f"/api/expectations/{intr['id']}", json={"actual_override": 15_200})
    assert r.status_code == 400 and "comes from" in r.json()["detail"]
    intr = prep.patch(f"/api/expectations/{intr['id']}", json={
        "actual_override": 15_200, "actual_note": "Interest account only; 3,300 is fees"}).json()[
        "expectations"][2]
    assert intr["actual"] == 15_200 and intr["outcome"] == "within"
    # A difference no one can explain is a misstatement, and goes to the dashboard.
    prep.patch(f"/api/expectations/{dep['id']}", json={"inputs": {"rate_pct": 5}})
    x = prep.patch(f"/api/expectations/{dep['id']}", json={"misstatement": True}).json()
    assert x["expectations"][0]["outcome"] == "misstatement"
    assert x["summary"]["misstatements"] == 6000
    assert any("misstatement" in s for s in steps(prep, eid))

    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert list(book) == ["About", "Analytics"]
    assert book["Analytics"]["Expected"].tolist() == [6000, 600000, 15000]
    files = prep.get(f"/api/engagements/{eid}/pack", params={"files": False})
    assert files.status_code == 200
    acts = [x["action"] for x in a.get("/api/activity").json()]
    assert {"expectation.create", "expectation.update", "expectation.download"} <= set(acts)


def test_locked_files_rights_and_rolling_forward():
    a, prep, rev, partner, eid, _ = team()
    books(prep, eid)
    url = f"/api/engagements/{eid}/expectations"
    r = prep.post(url, json={"kind": "payroll", "inputs": {"units": 10, "price": 5000},
                             "basis": "HR headcount report", "threshold": 1000})
    xid = r.json()["expectations"][0]["id"]
    rev.patch(f"/api/expectations/{xid}", json={"conclusion": "Fine.", "reviewed": True})
    assert prep.delete(f"/api/expectations/{xid}").status_code == 403

    r = rev.post(f"/api/engagements/{eid}/roll-forward", json={"name": "FY2027"})
    new = r.json()["id"]
    carried = prep.get(f"/api/engagements/{new}/expectations").json()["expectations"][0]
    assert carried["kind"] == "payroll" and carried["basis"] == "HR headcount report"
    assert carried["inputs"] == {"units_label": "Average headcount",
                                 "price_label": "Average monthly pay per person", "periods": 12}
    assert carried["conclusion"] == "" and carried["reviewed_at"] is None

    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert prep.post(url, json={"kind": "analytic"}).status_code == 409
    assert rev.patch(f"/api/expectations/{xid}", json={"reviewed": False}).status_code == 409
    assert prep.get(url).status_code == 200
    assert prep.get(f"{url}/export").status_code == 200


def test_bad_inputs_are_refused_and_no_trial_balance_is_said():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/expectations"
    assert prep.post(url, json={"kind": "analytic", "line": "nope"}).status_code == 422
    assert prep.post(url, json={"kind": "interest",
                                "inputs": {"rate_pct": -1}}).status_code == 422
    out = prep.post(url, json={"kind": "analytic", "method": "amount",
                               "inputs": {"amount": 100}}).json()
    assert out["problem"].startswith("Upload this year's trial balance")
    assert out["expectations"][0]["outcome"] == "no_actual"
