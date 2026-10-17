"""The IFRS 16 lease calculator, the IFRS 9 credit loss matrix, and the round 7 work now in
the audit file pack and carried by roll-forward."""

import io
import re
import zipfile

import pandas as pd
import pytest

from app import ecl, leases
from tests.test_risks_aging import AR
from tests.test_statements import client, engagement, map_all

LEASE = {"name": "Head office", "start_date": "2026-01-01", "term_months": 12,
         "payment": 1000, "frequency": "monthly", "timing": "arrears", "rate_pct": 12}


def test_a_lease_in_arrears_is_recalculated():
    eid = engagement(with_prior=False)
    r = client.post(f"/api/engagements/{eid}/leases",
                    json={**LEASE, "client_liability": 1500, "client_rou": 0})
    assert r.status_code == 201, r.text
    [x] = r.json()["leases"]
    assert x["first_liability"] == 11255.08
    assert x["end_date"] == "2027-01-01"
    # The last payment falls on 1 January, so it is still owed at the year end.
    assert x["liability"] == 1000.0 and x["current"] == 1000.0 and x["noncurrent"] == 0
    assert x["rou"] == 0 and x["depreciation"] == 11255.08
    assert x["interest"] == round(12000 - 11255.08, 2)
    assert x["payments"] == 11000
    assert x["diff_liability"] == 500 and x["flags"] == ["liability"]
    assert any("short-term" in n for n in x["notes"])
    later = client.post(f"/api/engagements/{eid}/leases",
                        json={**LEASE, "name": "Later", "start_date": "2027-03-01"}).json()
    y = next(x for x in later["leases"] if x["name"] == "Later")
    assert y["liability"] == y["rou"] == y["interest"] == y["depreciation"] == 0


def test_a_lease_in_advance_and_changes():
    eid = engagement(with_prior=False)
    body = {**LEASE, "term_months": 24, "payment": 500, "timing": "advance", "rate_pct": 0,
            "direct_costs": 0}
    out = client.post(f"/api/engagements/{eid}/leases", json=body).json()
    x = out["leases"][0]
    assert x["first_liability"] == 12000 and x["liability"] == 6000 and x["current"] == 6000
    assert x["rou"] == 6000 and x["interest"] == 0 and x["notes"] == []
    assert out["totals"]["liability"] == 6000 and out["totals"]["client_liability"] is None

    bad = client.post(f"/api/engagements/{eid}/leases",
                      json={**body, "term_months": 13, "frequency": "quarterly"})
    assert bad.status_code == 400
    r = client.put(f"/api/leases/{x['id']}", json={**body, "direct_costs": 1200})
    assert r.json()["leases"][0]["rou_first"] == 13200
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/leases/export").content), sheet_name=None)
    assert {"About", "Leases", "Schedules"} <= set(book) and len(book["Schedules"]) == 24
    assert client.delete(f"/api/leases/{x['id']}").json()["leases"] == []
    acts = {a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": eid}).json()}
    assert {"lease.create", "lease.update", "lease.delete", "lease.download"} <= acts


def test_month_ends_stay_month_ends():
    from datetime import date  # noqa: PLC0415
    assert leases.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert leases.add_months(date(2026, 1, 31), 13) == date(2027, 2, 28)
    assert leases.add_months(date(2026, 12, 31), -12) == date(2025, 12, 31)


def ar_engagement():
    eid = engagement(with_prior=False)
    map_all(eid)
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "ar"},
                    files={"file": ("ar.csv", AR, "text/csv")})
    assert r.status_code == 201, r.text
    return eid, r.json()["id"]


def test_the_credit_loss_matrix():
    eid, ar = ar_engagement()
    first = client.get(f"/api/engagements/{eid}/ecl").json()
    assert first["result"] is None and first["listings"][0]["id"] == ar
    rates = {"current": 1, "d1_30": 5, "d31_60": 10, "d61_90": 20, "d91_180": 50,
             "over_180": 100, "no_date": 100}
    r = client.put(f"/api/engagements/{eid}/ecl",
                   json={"upload_id": ar, "rates": rates, "booked": 1000})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    by = {x["id"]: x for x in res["rows"]}
    # 500,000 is 61 days overdue, 200,000 not yet due, 250,000 over 180 days overdue; the
    # credit note isn't provided for, and January's invoice is dated after the period end.
    assert by["d61_90"]["allowance"] == 100000 and by["current"]["allowance"] == 2007
    assert by["over_180"]["allowance"] == 250000
    assert res["expected"] == 352007 and res["difference"] == 1000 - 352007
    assert res["level"] == "above_trivial"
    assert any("Credit balances" in n for n in res["notes"])

    r = client.put(f"/api/engagements/{eid}/ecl",
                   json={"upload_id": ar, "rates": rates, "forward_pct": 10})
    assert r.json()["result"]["rows"][0]["rate"] == 1.1
    assert client.put(f"/api/engagements/{eid}/ecl",
                      json={"rates": {"d999": 5}}).status_code == 400
    assert client.put(f"/api/engagements/{eid}/ecl", json={"upload_id": 10**6}).status_code == 400
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/ecl/export").content), sheet_name=None)
    assert {"About", "Provision matrix", "Allowance accounts"} <= set(book)


def test_default_rates_are_flagged():
    eid, ar = ar_engagement()
    res = client.put(f"/api/engagements/{eid}/ecl", json={"upload_id": ar}).json()["result"]
    assert any("starting rates" in n for n in res["notes"])
    assert ecl.ALLOWANCE_WORDS.search("Allowance for doubtful accounts")
    assert not ecl.ALLOWANCE_WORDS.search("Accounts Receivable")


def test_the_pack_and_roll_forward_carry_the_newer_work():
    eid, ar = ar_engagement()
    client.post(f"/api/engagements/{eid}/leases",
                json={**LEASE, "term_months": 36, "client_liability": 5})
    client.put(f"/api/engagements/{eid}/ecl", json={"upload_id": ar, "booked": 10})
    client.put(f"/api/engagements/{eid}/vat", json={"rate_pct": 5, "output_accounts": ["2200"]})
    client.post(f"/api/engagements/{eid}/vat/returns", json={
        "period_start": "2026-01-01", "period_end": "2026-03-31", "taxable_sales": 10})
    plan = client.put(f"/api/engagements/{eid}/planning", json={
        "addressee": "The Board", "firm_name": "Heiba & Co", "fee": 5000, "currency": "EGP"})
    assert plan.status_code == 200, plan.text

    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/engagements/{eid}/pack").content))
    names = " ".join(z.namelist())
    assert "leases" in names and "expected-credit-losses" in names
    assert "vat-reconciliation" in names

    r = client.post(f"/api/engagements/{eid}/roll-forward", json={})
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["carried"]["leases"] == 1 and new["carried"]["ecl"] and new["carried"]["vat"]
    assert new["carried"]["plan"]
    [x] = client.get(f"/api/engagements/{new['id']}/leases").json()["leases"]
    assert x["client_liability"] is None
    v = client.get(f"/api/engagements/{new['id']}/vat").json()["setup"]
    assert v["rate_pct"] == 5 and v["upload_id"] is None
    p = client.get(f"/api/engagements/{new['id']}/planning").json()
    assert p["firm_name"] == "Heiba & Co" and p["fee"] is None
    sections = {s["id"]: s for s in client.get(
        f"/api/engagements/{new['id']}/review").json()["sections"]}
    assert sections["estimates"]["needed"]


def test_group_components_follow_their_successors():
    parent = engagement(with_prior=False)
    sub = engagement(with_prior=False, client_name="Subsidiary")
    client.post(f"/api/engagements/{parent}/group/components",
                json={"component_engagement_id": sub, "ownership_pct": 60})
    r = client.post(f"/api/engagements/{parent}/roll-forward", json={})
    assert r.json()["carried"]["group_components"] == 0
    assert r.json()["carried"]["group_components_left"] == 1
    sub_next = client.post(f"/api/engagements/{sub}/roll-forward", json={}).json()
    r = client.post(f"/api/engagements/{parent}/roll-forward", json={"name": "Again"})
    assert r.json()["carried"]["group_components"] == 1
    g = client.get(f"/api/engagements/{r.json()['id']}/group").json()
    assert g["components"][0]["component_engagement_id"] == sub_next["id"]
    assert g["components"][0]["ownership_pct"] == 60


@pytest.mark.parametrize("module", [leases, ecl])
def test_new_text_has_arabic(module):
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    source = open(module.__file__, encoding="utf-8").read()
    texts = re.findall(r'HTTPException\(\d+, f?"([^"]+)"\)', source)
    texts = [re.sub(r"\{[^}]+\}", "9", t) for t in texts]
    texts += [v[0] for v in getattr(module, "FREQUENCIES", {}).values()]
    texts += list(getattr(module, "TIMINGS", {}).values())
    missing = sorted({t for t in texts if not translated(t)})
    assert not missing, missing
