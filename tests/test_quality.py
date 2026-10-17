"""The engagement quality review: set up by a partner, filled in and signed off by the named
reviewer, who sees the file without joining the team."""

import io

import pandas as pd
import pytest

from tests.test_auth import add_user
from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")
ANSWERS = {q: {"answer": "yes"} for q in (
    "risks", "materiality", "judgments", "misstatements", "independence", "consultation",
    "disclosures", "report", "governance")} | {"unresolved": {"answer": "no"}}


def url(eid, rest=""):
    return f"/api/engagements/{eid}/quality-review{rest}"


def test_the_quality_review_from_set_up_to_sign_off():
    a, prep, rev, partner, eid, _ = team()
    eq = add_user(a, "partner", "eqr@example.com")
    eq_id = eq.get("/api/auth/me").json()["id"]
    assert eq.get(url(eid)).status_code == 404  # not on the team, not named yet

    s = prep.get(url(eid)).json()
    assert not s["required"] and s["blockers"] == ["Say that this engagement needs a quality review first."]
    assert prep.put(url(eid, "/setup"), json={"required": True}).status_code == 403
    r = partner.put(url(eid, "/setup"), json={"required": True})
    assert r.status_code == 400 and "why" in r.json()["detail"]
    r = partner.put(url(eid, "/setup"), json={"required": True, "reason": "listed",
                                              "reviewer_id": prep.get("/api/auth/me").json()["id"]})
    assert r.status_code == 400 and "partner or admin" in r.json()["detail"]
    s = partner.put(url(eid, "/setup"), json={"required": True, "reason": "listed",
                                              "reviewer_id": eq_id}).json()
    assert (s["reviewer_name"], s["reviewer_on_team"], s["reason_label"]) == \
        ("Partner", False, "Listed entity")

    # Named, the reviewer now sees the file and its list, but only they fill it in.
    assert eq.get(f"/api/engagements/{eid}").status_code == 200
    assert eid in {e["id"] for c in eq.get("/api/clients").json() for e in c["engagements"]}
    assert partner.put(url(eid), json={"conclusion": "Fine"}).status_code == 403
    assert rev.put(url(eid), json={"conclusion": "Fine"}).status_code == 403
    r = eq.post(url(eid, "/signoff"), json={})
    assert r.status_code == 400 and "10 left" in r.json()["detail"]
    s = eq.put(url(eid), json={"answers": {**ANSWERS, "unresolved": {"answer": "yes"}},
                               "conclusion": "No matters remain."}).json()
    assert s["answered"] == 10 and s["flags"] == ["unresolved"] and s["blockers"] == []
    r = eq.post(url(eid, "/signoff"), json={})
    assert r.status_code == 400 and "note" in r.json()["detail"]
    assert partner.post(url(eid, "/signoff"), json={"note": "x"}).status_code == 403
    s = eq.post(url(eid, "/signoff"), json={"note": "Settled with the partner."}).json()
    assert s["signed_by"] == "Partner" and not s["changed_since_signoff"]
    assert prep.get(f"/api/engagements/{eid}/dashboard").json()["quality"]["signed"]

    # A change afterwards needs a fresh look; saving the same answers doesn't.
    assert not eq.put(url(eid), json={"answers": ANSWERS | {"unresolved": {"answer": "yes"}}}
                      ).json()["changed_since_signoff"]
    assert eq.put(url(eid), json={"answers": ANSWERS}).json()["changed_since_signoff"]
    steps = prep.get(f"/api/engagements/{eid}/dashboard").json()["next_steps"]
    assert any("after its sign-off" in x["text"] and x["panel"] == "qr-panel" for x in steps)

    book = pd.read_excel(io.BytesIO(prep.get(url(eid, "/export")).content), sheet_name=None)
    assert list(book) == ["About", "Checklist"] and len(book["Checklist"]) == 10

    # A new reviewer signs off afresh, and the old one loses sight of the file.
    me = partner.get("/api/auth/me").json()["id"]
    s = partner.put(url(eid, "/setup"), json={"reviewer_id": me}).json()
    assert s["signed_by"] == "" and s["reviewer_on_team"]
    assert eq.get(f"/api/engagements/{eid}").status_code == 404
    acts = [x["action"] for x in a.get("/api/activity", params={"engagement_id": eid}).json()]
    assert {"quality.setup", "quality.update", "quality.signoff", "quality.download"} <= set(acts)


def test_taking_the_sign_off_back_and_the_report_warning():
    a, prep, rev, partner, eid, _ = team()
    me = partner.get("/api/auth/me").json()["id"]
    partner.put(url(eid, "/setup"), json={"required": True, "reason": "risk", "reviewer_id": me})
    partner.put(url(eid), json={"answers": ANSWERS, "conclusion": "Agreed."})
    assert partner.post(url(eid, "/signoff"), json={}).status_code == 200
    assert rev.delete(url(eid, "/signoff")).status_code == 403
    assert a.delete(url(eid, "/signoff")).status_code == 200
    assert a.delete(url(eid, "/signoff")).status_code == 400
    assert partner.post(url(eid, "/signoff"), json={}).status_code == 200

    # Not needed any more: the reviewer's checklist stays on file but asks nothing.
    s = partner.put(url(eid, "/setup"), json={"required": False}).json()
    assert not s["required"]
    assert prep.get(f"/api/engagements/{eid}/dashboard").json()["quality"]["required"] is False


def test_roll_forward_carries_the_set_up_only():
    a, prep, rev, partner, eid, _ = team()
    me = partner.get("/api/auth/me").json()["id"]
    partner.put(url(eid, "/setup"), json={"required": True, "reason": "law",
                                          "reason_note": "Bank", "reviewer_id": me})
    partner.put(url(eid), json={"answers": ANSWERS, "conclusion": "Agreed."})
    new = a.post(f"/api/engagements/{eid}/roll-forward", json={}).json()["id"]
    s = a.get(url(new)).json()
    assert (s["required"], s["reason"], s["reason_note"], s["reviewer_id"]) == (True, "law", "Bank", me)
    assert s["answered"] == 0 and s["conclusion"] == "" and not s["signed_by"]
    other = a.post(f"/api/engagements/{eid}/roll-forward",
                   json={"quality": False, "name": "Another"}).json()["id"]
    assert not a.get(url(other)).json()["required"]
