"""The internal controls register with tests of controls, and external confirmations."""

import io
from datetime import date, timedelta

import pandas as pd
import pytest

from app import controls, db
from tests.test_review import section, team
from tests.test_risks_aging import AR
from tests.test_statements import client, engagement

CONTROL = {"area": "revenue", "title": "Credit notes approved by the finance manager",
           "process": "Sales", "frequency": "daily", "owner": "Finance manager"}


def actions(eid):
    with db.SessionLocal() as s:
        return [a.action for a in s.query(db.Activity).filter_by(engagement_id=eid)]


def steps(eid):
    return [s["text"] for s in client.get(f"/api/engagements/{eid}/dashboard").json()["next_steps"]]


# --- Attribute sampling -------------------------------------------------------------


@pytest.mark.parametrize("confidence,tolerable,size", [(95, 5, 59), (90, 5, 45), (95, 10, 29),
                                                        (90, 10, 22)])
def test_sample_sizes_with_no_expected_deviations_match_the_standard_tables(confidence,
                                                                             tolerable, size):
    assert controls.sample_size(confidence, tolerable, 0)["sample_size"] == size


def test_more_expected_deviations_need_a_bigger_sample():
    sizes = [controls.sample_size(95, 5, e)["sample_size"] for e in (0, 0.5, 1, 2)]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]
    r = controls.sample_size(95, 5, 1)
    # With the deviations allowed, the upper limit still comes in under the tolerable rate.
    assert controls.upper_limit(r["allowed_deviations"], r["sample_size"], 95) <= 5
    for bad in ((80, 5, 0), (95, 0, 0), (95, 5, 5), (95, 60, 0), (95, 5, -1)):
        with pytest.raises(ValueError):
            controls.sample_size(*bad)
    with pytest.raises(ValueError, match="more than"):
        controls.sample_size(95, 1, 0.9)


def test_finding_the_expected_deviations_still_passes():
    r = controls.sample_size(95, 5, 1)
    assert (r["sample_size"], r["allowed_deviations"]) == (93, 1)  # as the published tables
    assert controls.upper_limit(1, 93, 95) <= 5


def test_upper_deviation_limit():
    assert controls.upper_limit(0, 59, 95) == pytest.approx(4.95, abs=0.01)
    assert controls.upper_limit(1, 59, 95) == pytest.approx(7.7, abs=0.1)
    assert controls.upper_limit(3, 3, 95) == 100.0
    assert controls.pick_items(10, 20, 1) == list(range(1, 11))
    picked = controls.pick_items(1000, 25, 7)
    assert picked == controls.pick_items(1000, 25, 7) and len(set(picked)) == 25
    assert picked == sorted(picked) and 1 <= picked[0] and picked[-1] <= 1000


def test_sample_size_route():
    r = client.get("/api/controls/sample-size", params={"confidence": 95, "tolerable": 5})
    assert r.json() == {"sample_size": 59, "expected_deviations": 0.0, "allowed_deviations": 0}
    r = client.get("/api/controls/sample-size", params={"confidence": 95, "tolerable": 5,
                                                         "expected": 6})
    assert r.status_code == 400 and "below the tolerable rate" in r.json()["detail"]


# --- Controls register -----------------------------------------------------------


def test_record_assess_and_test_controls():
    eid = engagement(with_prior=False)
    risk = client.post(f"/api/engagements/{eid}/risks", json={
        "area": "revenue", "title": "Revenue overstated", "level": "significant"}).json()
    r = client.post(f"/api/engagements/{eid}/controls", json={**CONTROL, "risk_id": risk["id"]})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["risk_title"] == "Revenue overstated" and c["frequency_label"] == "Daily"
    assert c["design"] == "not_assessed" and c["result"] == "not_tested" and c["key"]
    other = client.post(f"/api/engagements/{eid}/risks", json={
        "area": "fraud", "title": "Override", "level": "significant"})
    bad = client.post(f"/api/engagements/{eid}/controls", json={**CONTROL, "risk_id": 9999})
    assert bad.status_code == 400 and other.status_code == 201
    assert "Assess the design of 1 control." in steps(eid)

    # Design: effective, so the key control is now waiting to be tested.
    r = client.patch(f"/api/controls/{c['id']}", json={
        "design": "effective", "walkthrough": "W-1", "design_note": "Walked through one note."})
    assert r.json()["design_label"] == "Designed and implemented"
    assert "Test 1 key control you plan to rely on." in steps(eid)

    # A daily control, sized by frequency: 20 items, picked from the 250 days it ran.
    plan = client.post(f"/api/controls/{c['id']}/plan",
                       json={"method": "frequency", "population": 250}).json()
    assert plan["sample_size"] == 20 and len(plan["selected"]) == 20 and plan["seed"]
    assert client.post(f"/api/controls/{c['id']}/result", json={
        "tested": 20, "deviations": 1}).status_code == 400  # needs a description
    assert client.post(f"/api/controls/{c['id']}/result", json={
        "tested": 5, "deviations": 0}).status_code == 400  # fewer than planned
    assert client.post(f"/api/controls/{c['id']}/result", json={
        "tested": 21, "deviations": 22, "deviation_note": "x"}).status_code == 400
    r = client.post(f"/api/controls/{c['id']}/result", json={
        "tested": 20, "deviations": 1, "deviation_note": "One note not approved.",
        "workpaper": "T-1"}).json()
    assert r["result"] == "not_effective" and r["tested_by"] and r["upper_limit"] is None
    assert "Consider 1 control deficiency: report significant ones to those charged with " \
           "governance." in steps(eid)

    # Statistical: 95% confidence, 5% tolerable, none expected: 59 items, none deviating.
    plan = client.post(f"/api/controls/{c['id']}/plan", json={
        "method": "statistical", "confidence": 95, "tolerable_rate": 5, "population": 5000}).json()
    assert plan["sample_size"] == 59 and len(plan["selected"]) == 59
    assert plan["result"] == "not_tested" and plan["tested"] is None  # planning clears it
    r = client.post(f"/api/controls/{c['id']}/result", json={"tested": 59, "deviations": 0}).json()
    assert r["result"] == "effective" and r["upper_limit"] == pytest.approx(4.95, abs=0.01)
    r = client.post(f"/api/controls/{c['id']}/result", json={
        "tested": 59, "deviations": 1, "deviation_note": "One"}).json()
    assert r["result"] == "not_effective" and r["upper_limit"] > 5
    # A sample size of its own, and a small population tested in full.
    plan = client.post(f"/api/controls/{c['id']}/plan", json={
        "method": "statistical", "tolerable_rate": 5, "population": 30, "sample_size": 40}).json()
    assert plan["sample_size"] == 30 and plan["selected"] == list(range(1, 31))
    r = client.post(f"/api/controls/{c['id']}/result", json={"tested": 30, "deviations": 0}).json()
    assert r["result"] == "effective" and r["upper_limit"] == 0
    assert client.post(f"/api/controls/{c['id']}/result",
                       json={"tested": 31, "deviations": 0}).status_code == 400

    auto = client.post(f"/api/engagements/{eid}/controls", json={
        "area": "revenue", "title": "System blocks duplicate invoices", "nature": "automated",
        "key": False}).json()
    assert client.post(f"/api/controls/{auto['id']}/plan", json={
        "method": "statistical", "tolerable_rate": 5}).status_code == 400
    assert client.post(f"/api/controls/{auto['id']}/result",
                       json={"tested": 1, "deviations": 0}).status_code == 400  # not planned
    assert client.post(f"/api/controls/{auto['id']}/plan",
                       json={"method": "frequency"}).json()["sample_size"] == 1

    reg = client.get(f"/api/engagements/{eid}/controls").json()
    assert [x["title"] for x in reg["controls"]] == [CONTROL["title"], auto["title"]]  # key first
    assert reg["summary"] == {"controls": 2, "key": 1,
                              "design": {"not_assessed": 1, "effective": 1, "deficient": 0},
                              "tested": 1, "effective": 1, "not_effective": 0, "key_to_test": 0,
                              "deficient": 0}

    # Removing the risk unlinks the control.
    assert client.delete(f"/api/risks/{risk['id']}").status_code == 204
    reg = client.get(f"/api/engagements/{eid}/controls").json()
    assert reg["controls"][0]["risk_id"] is None and reg["controls"][0]["risk_title"] is None
    # The link can be set and cleared.
    r = client.patch(f"/api/controls/{c['id']}", json={"risk_id": other.json()["id"]})
    assert r.json()["risk_title"] == "Override"
    assert client.patch(f"/api/controls/{c['id']}", json={"risk_id": None}).json()["risk_id"] is None

    xl = client.get(f"/api/engagements/{eid}/controls/export")
    assert xl.status_code == 200
    sheets = pd.read_excel(io.BytesIO(xl.content), sheet_name=None)
    assert list(sheets) == ["About", "Controls register", "Tests of controls"]
    assert sheets["Tests of controls"]["Sample size"].tolist() == [30, 1]
    assert client.delete(f"/api/controls/{auto['id']}").status_code == 204
    assert {"control.create", "control.design", "control.plan", "control.test",
            "control.update", "control.delete", "control.download"} <= set(actions(eid))
    assert section(client, eid, "controls")["needed"]


@pytest.mark.usefixtures("real_auth")
def test_preparers_cannot_remove_controls_and_roll_forward_carries_them():
    a, prep, rev, _, eid, _ = team()
    c = prep.post(f"/api/engagements/{eid}/controls", json=CONTROL)
    assert c.status_code == 201
    prep.patch(f"/api/controls/{c.json()['id']}", json={"design": "deficient"})
    assert prep.delete(f"/api/controls/{c.json()['id']}").status_code == 403
    new = rev.post(f"/api/engagements/{eid}/roll-forward", json={"trial_balance": False})
    assert new.status_code == 201 and new.json()["carried"]["controls"] == 1
    carried = rev.get(f"/api/engagements/{new.json()['id']}/controls").json()["controls"]
    assert [(x["title"], x["design"], x["result"]) for x in carried] == [
        (CONTROL["title"], "not_assessed", "not_tested")]
    assert carried[0]["source"].startswith("rolled:")
    assert section(rev, new.json()["id"], "controls")["needed"]
    none = rev.post(f"/api/engagements/{eid}/roll-forward",
                    json={"name": "Other", "trial_balance": False, "controls": False})
    assert "controls" not in none.json()["carried"]


# --- External confirmations --------------------------------------------------------


def upload_ar(eid):
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "ar"},
                    files={"file": ("ar.csv", AR, "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_track_confirmations_from_sending_to_the_reply():
    eid = engagement(with_prior=False)
    client.put(f"/api/engagements/{eid}/materiality", json={
        "benchmark": "revenue", "benchmark_amount": 1_000_000, "trivial_pct": 5})
    trivial = client.get(f"/api/engagements/{eid}").json()["materiality"]["trivial_threshold"]
    bank = client.post(f"/api/engagements/{eid}/confirmations", json={
        "kind": "bank", "party": "First Bank", "reference": "001-22", "balance": 10_000,
        "contact": "audit@firstbank.example"})
    assert bank.status_code == 201, bank.text
    b = bank.json()
    assert b["outcome"] == "to_send" and b["open"] and b["kind_label"] == "Bank"
    assert "Send 1 confirmation request." in steps(eid)

    sent = client.patch(f"/api/confirmations/{b['id']}", json={"status": "sent"}).json()
    assert sent["sent_on"] == date.today().isoformat() and sent["outcome"] == "waiting"
    old = (date.today() - timedelta(days=20)).isoformat()
    over = client.patch(f"/api/confirmations/{b['id']}", json={"sent_on": old}).json()
    assert over["outcome"] == "overdue"
    assert "Send a reminder for 1 confirmation request with no reply." in steps(eid)
    early = (date.today() - timedelta(days=30)).isoformat()
    assert client.patch(f"/api/confirmations/{b['id']}",
                        json={"reminder_on": early}).status_code == 400
    assert client.patch(f"/api/confirmations/{b['id']}",
                        json={"reminder_on": date.today().isoformat()}).json()["outcome"] == "waiting"

    # The reply: a difference above clearly trivial needs explaining.
    assert client.patch(f"/api/confirmations/{b['id']}",
                        json={"status": "replied"}).status_code == 400  # what did it confirm?
    r = client.patch(f"/api/confirmations/{b['id']}", json={
        "status": "replied", "confirmed": 10_000 - trivial - 100}).json()
    assert r["outcome"] == "difference" and r["difference"] == -(trivial + 100)
    assert r["received_on"] == date.today().isoformat()
    assert "Explain 1 difference in confirmation replies." in steps(eid)
    r = client.patch(f"/api/confirmations/{b['id']}", json={
        "difference_note": "Cheque in transit, cleared 3 January."}).json()
    assert r["outcome"] == "explained" and not r["open"]
    small = client.patch(f"/api/confirmations/{b['id']}", json={
        "difference_note": "", "confirmed": 10_000 - trivial / 2}).json()
    assert small["outcome"] == "explained"  # clearly trivial
    assert client.patch(f"/api/confirmations/{b['id']}",
                        json={"confirmed": 10_000}).json()["outcome"] == "agreed"

    # No reply: a positive request needs alternative procedures; a negative one doesn't.
    lawyer = client.post(f"/api/engagements/{eid}/confirmations", json={
        "kind": "legal", "party": "Smith & Co"}).json()
    r = client.patch(f"/api/confirmations/{lawyer['id']}", json={"status": "no_reply"}).json()
    assert r["outcome"] == "alternative_needed" and r["sent_on"]
    assert "Do alternative procedures for 1 confirmation with no reply." in steps(eid)
    r = client.patch(f"/api/confirmations/{lawyer['id']}", json={
        "alternative": "Reviewed board minutes and legal invoices."}).json()
    assert r["outcome"] == "alternative_done"
    neg = client.post(f"/api/engagements/{eid}/confirmations", json={
        "kind": "receivable", "party": "Small customer", "form": "negative", "balance": 50}).json()
    assert client.patch(f"/api/confirmations/{neg['id']}",
                        json={"status": "no_reply"}).json()["outcome"] == "negative_no_reply"
    blank = client.post(f"/api/engagements/{eid}/confirmations", json={
        "kind": "payable", "party": "Supplier", "form": "blank"}).json()
    assert client.patch(f"/api/confirmations/{blank['id']}",
                        json={"status": "replied"}).status_code == 400  # what did they say?
    client.delete(f"/api/confirmations/{blank['id']}")
    quiet = client.patch(f"/api/confirmations/{neg['id']}", json={"status": "sent", "sent_on": old})
    assert quiet.json()["outcome"] == "negative_no_reply"  # never chased
    # Back to "to send" clears the dates.
    r = client.patch(f"/api/confirmations/{neg['id']}", json={"status": "to_send"}).json()
    assert r["sent_on"] is None and r["received_on"] is None

    s = client.get(f"/api/engagements/{eid}/confirmations").json()["summary"]
    assert (s["requests"], s["sent"], s["replied"], s["response_rate"]) == (3, 2, 1, 50.0)
    assert s["open"] == 1 and s["outcomes"]["agreed"] == 1
    assert [k["kind"] for k in s["by_kind"]] == ["bank", "receivable", "legal"]
    assert s["by_kind"][0]["covered_pct"] == 100.0

    xl = client.get(f"/api/engagements/{eid}/confirmations/export")
    sheets = pd.read_excel(io.BytesIO(xl.content), sheet_name=None)
    assert list(sheets) == ["About", "Coverage", "Confirmations"]
    assert sheets["Confirmations"]["Party"].tolist() == ["First Bank", "Small customer",
                                                         "Smith & Co"]
    assert {"confirmation.create", "confirmation.update",
            "confirmation.download"} <= set(actions(eid))


def test_pick_requests_from_a_receivables_listing():
    eid = engagement(with_prior=False)
    ar = upload_ar(eid)
    url = f"/api/engagements/{eid}/confirmations/from-listing"
    assert client.post(url, json={"upload_id": ar}).status_code == 400  # which balances?
    r = client.post(url, json={"upload_id": ar, "largest": 2})
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["added"], out["skipped"]) == (2, 0)
    assert [(c["party"], c["reference"], c["balance"], c["kind"]) for c in out["confirmations"]] == [
        ("Alpha", "C1", 700000.0, "receivable"), ("Beta", "C2", 248000.0, "receivable")]
    r = client.post(url, json={"upload_id": ar, "min_balance": 500}).json()
    assert (r["added"], r["skipped"]) == (1, 2)  # Gamma is new; Alpha and Beta are there
    tb = client.get(f"/api/engagements/{eid}").json()["uploads"][0]["id"]
    assert client.post(url, json={"upload_id": tb, "largest": 1}).status_code == 400


@pytest.mark.usefixtures("real_auth")
def test_who_can_remove_a_request_and_locking():
    a, prep, rev, partner, eid, _ = team()
    c = prep.post(f"/api/engagements/{eid}/confirmations",
                  json={"kind": "other", "party": "Landlord"}).json()
    d = prep.post(f"/api/engagements/{eid}/confirmations",
                  json={"kind": "other", "party": "Insurer"}).json()
    prep.patch(f"/api/confirmations/{c['id']}", json={"status": "sent"})
    assert prep.delete(f"/api/confirmations/{c['id']}").status_code == 403
    assert prep.delete(f"/api/confirmations/{d['id']}").status_code == 204
    assert rev.delete(f"/api/confirmations/{c['id']}").status_code == 204
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    r = prep.post(f"/api/engagements/{eid}/confirmations", json={"kind": "other", "party": "X"})
    assert r.status_code == 409
    assert prep.get(f"/api/engagements/{eid}/confirmations").status_code == 200
