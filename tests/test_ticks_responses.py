"""Tick marks and cross-references on the lead sheets, and management responses to findings
through the client portal."""

import io
from datetime import date, timedelta

import pandas as pd

from tests.test_auth import add_user
from tests.test_portal import setup
from tests.test_statements import client, engagement


def test_ticks_use_the_firm_legend_and_go_in_the_lead_sheets():
    eid = engagement(with_prior=False)
    data = client.get(f"/api/engagements/{eid}/ticks").json()
    assert data["ticks"] == [] and "TB" in [m["symbol"] for m in data["legend"]]
    finding = client.post(f"/api/engagements/{eid}/findings",
                          json={"detail": "Cut-off error"}).json()
    refs = client.get(f"/api/engagements/{eid}/ticks").json()["refs"]
    link = next(r["link"] for r in refs if r["link"] == f"finding:{finding['id']}")

    r = client.post(f"/api/engagements/{eid}/ticks",
                    json={"account": "4000", "symbol": "X", "reference": "C-3", "link": link})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["meaning"].startswith("Exception") and t["link_label"].startswith("Finding #")
    assert client.post(f"/api/engagements/{eid}/ticks",
                       json={"account": "4000", "symbol": "??"}).status_code == 400
    assert client.post(f"/api/engagements/{eid}/ticks",
                       json={"account": "4000", "symbol": "TB",
                             "link": "finding:99999"}).status_code == 400
    for bad in ("finding:99999999999999999999", "finding:\u00b2", "nothing:1"):
        assert client.post(f"/api/engagements/{eid}/ticks",
                           json={"account": "4000", "symbol": "TB", "link": bad}).status_code == 400

    # A new legend entry can be used; a used tick keeps its meaning after it leaves the legend.
    legend = client.post("/api/tick-marks", json={"symbol": "V", "meaning": "Vouched"}).json()
    assert client.post("/api/tick-marks", json={"symbol": "V", "meaning": "x"}).status_code == 409
    x = next(m for m in legend if m["symbol"] == "X")
    client.delete(f"/api/tick-marks/{x['id']}")
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/statements/export").content), sheet_name=None)
    assert book["Tick marks"]["Tick"].tolist() == ["X"]
    assert book["Tick legend"]["Meaning"].tolist()[0].startswith("Exception")

    assert client.delete(f"/api/ticks/{t['id']}").status_code == 204
    assert client.get(f"/api/engagements/{eid}/ticks").json()["ticks"] == []
    acts = [a["action"] for a in client.get("/api/activity", params={"engagement_id": eid}).json()]
    assert {"tick.add", "tick.delete"} <= set(acts)


def test_only_partners_change_the_legend(real_auth):
    from tests.test_auth import admin
    a = admin()
    prep = add_user(a, "preparer")
    assert prep.get("/api/tick-marks").status_code == 200
    assert prep.post("/api/tick-marks", json={"symbol": "Q", "meaning": "x"}).status_code == 403


def test_management_responses_through_the_portal(real_auth):
    a, prep, rev, cl, acme, eid = setup()
    f = prep.post(f"/api/engagements/{eid}/findings",
                  json={"detail": "Credit notes not approved", "severity": "high"}).json()
    other = prep.post(f"/api/engagements/{eid}/findings", json={"detail": "Internal only"}).json()
    # Not sent yet: the client can't see or answer it.
    assert cl.get("/api/portal/findings").json() == {"engagements": []}
    assert cl.put(f"/api/findings/{f['id']}/response",
                  json={"response": "x"}).status_code == 404

    r = prep.put(f"/api/findings/{f['id']}/response-request",
                 json={"recommendation": "Approve every credit note."})
    assert r.status_code == 200 and r.json()["state"] == "waiting"
    [e] = cl.get("/api/portal/findings").json()["engagements"]
    assert [x["id"] for x in e["findings"]] == [f["id"]]
    assert "trail" not in e["findings"][0]
    assert cl.put(f"/api/findings/{other['id']}/response",
                  json={"response": "x"}).status_code == 404

    due = (date.today() - timedelta(days=1)).isoformat()
    r = cl.put(f"/api/findings/{f['id']}/response",
               json={"response": "Agreed", "action_plan": "New approval step",
                     "owner": "CFO", "due_on": due})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "overdue" and r.json()["from_client"]
    # Answered: it can't be withdrawn, and the finding now counts as worked.
    assert prep.delete(f"/api/findings/{f['id']}/response-request").status_code == 409
    got = prep.get(f"/api/findings/{f['id']}").json()["response"]
    assert got["owner"] == "CFO" and got["responded_by"] == "Cara Client"

    assert prep.put(f"/api/findings/{f['id']}/follow-up",
                    json={"status": "implemented"}).status_code == 400
    r = prep.put(f"/api/findings/{f['id']}/follow-up",
                 json={"status": "implemented", "note": "Saw approvals for March"})
    assert r.json()["follow_up_label"] == "Implemented" and r.json()["state"] == "answered"
    assert cl.put(f"/api/findings/{f['id']}/response",
                  json={"response": "Changed"}).status_code == 409
    seen = cl.get("/api/portal/findings").json()["engagements"][0]["findings"][0]
    assert "follow_up_note" not in seen and "asked_by" not in seen
    staff = prep.get(f"/api/engagements/{eid}/responses").json()["rows"][0]
    assert staff["follow_up_note"] == "Saw approvals for March"
    assert rev.delete(f"/api/findings/{f['id']}").status_code == 409

    summary = prep.get(f"/api/engagements/{eid}/responses").json()["summary"]
    assert summary == {"total": 1, "waiting": 0, "answered": 1, "overdue": 0}
    book = pd.read_excel(io.BytesIO(
        prep.get(f"/api/engagements/{eid}/responses/export").content), sheet_name="Responses")
    assert book["Owner"].tolist() == ["CFO"]
    acts = {x["action"] for x in a.get("/api/activity", params={"engagement_id": eid}).json()}
    assert {"finding.ask_client", "finding.response", "finding.follow_up"} <= acts


def test_withdraw_team_recorded_and_locked_files(real_auth):
    a, prep, rev, cl, acme, eid = setup()
    f = prep.post(f"/api/engagements/{eid}/findings", json={"detail": "Late bank recs"}).json()
    assert prep.put(f"/api/findings/{f['id']}/response",
                    json={"response": "x"}).status_code == 400  # not sent yet
    prep.put(f"/api/findings/{f['id']}/response-request", json={})
    assert prep.delete(f"/api/findings/{f['id']}/response-request").status_code == 204
    assert prep.get(f"/api/findings/{f['id']}").json()["response"] is None

    prep.put(f"/api/findings/{f['id']}/response-request", json={})
    r = prep.put(f"/api/findings/{f['id']}/response", json={"response": "Said in the meeting"})
    assert r.status_code == 200 and not r.json()["from_client"]

    assert a.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    r = cl.put(f"/api/findings/{f['id']}/response", json={"response": "Late"})
    assert r.status_code == 409
    assert cl.get("/api/portal/findings").json()["engagements"][0]["locked"] is True
