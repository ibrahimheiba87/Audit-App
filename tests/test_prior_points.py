"""Following up last year's points, and the notifications bell."""

import io
from datetime import date, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from tests.test_auth import add_user, join
from tests.test_related_summaries import steps
from tests.test_review import section, team

pytestmark = pytest.mark.usefixtures("real_auth")


def last_year(a, prep, rev, eid):
    """Findings and a management letter in this year's file, to carry into the next."""
    fids = []
    for text in ("Bank reconciliations not reviewed\nNo sign-off for six months.",
                 "Fixed asset register not updated", "Petty cash count short"):
        r = prep.post(f"/api/engagements/{eid}/findings",
                      json={"severity": "high", "detail": text})
        assert r.status_code == 201, r.text
        fids.append(r.json()["id"])
    # The third is cleared: it doesn't come forward.
    assert rev.patch(f"/api/findings/{fids[2]}",
                     json={"status": "cleared", "note": "Counted again"}).status_code == 200
    with db.SessionLocal() as s:
        s.add(db.AiDraft(engagement_id=eid, kind="letter", created_by="A", content={
            "intro": "", "closing": "", "points": [
                {"title": "Reconcile the bank monthly", "finding_ids": [fids[0]],
                 "observation": "Reconciliations were not reviewed.", "implication": "Errors.",
                 "recommendation": "Review each month.", "priority": "high"},
                {"title": "Segregate duties over payments", "finding_ids": [],
                 "observation": "One person pays and records.", "implication": "",
                 "recommendation": "Split the roles.", "priority": "medium"}]}))
        s.commit()
    return fids


def test_last_years_points_come_forward_and_are_followed_up():
    a, prep, rev, partner, eid, _ = team()
    fids = last_year(a, prep, rev, eid)
    r = rev.post(f"/api/engagements/{eid}/roll-forward", json={"name": "FY2027"})
    assert r.status_code == 201, r.text
    new = r.json()["id"]
    assert r.json()["carried"]["prior_points"] == 3
    url = f"/api/engagements/{new}/prior-points"
    out = prep.get(url).json()
    points = out["points"]
    # The letter's two points, and the open finding no letter point covers.
    assert [(p["origin"], p["title"]) for p in points] == [
        ("letter", "Reconcile the bank monthly"), ("letter", "Segregate duties over payments"),
        ("finding", "Fixed asset register not updated")]
    assert points[0]["detail"] == "Reconciliations were not reviewed.\n\nErrors."
    assert points[0]["first_raised"] == "FY2026" and points[0]["status"] == "to_follow_up"
    assert out["sources"] == [{"id": eid, "name": "FY2026", "period_end": "2026-12-31"}]
    assert any("Follow up 3 points raised last year" in s for s in steps(prep, new))
    assert fids  # the first finding came through its letter point

    bank, duties, assets = (p["id"] for p in points)
    r = prep.patch(f"/api/prior-points/{bank}", json={"status": "resolved"})
    assert r.status_code == 400 and "followed up" in r.json()["detail"]
    out = prep.patch(f"/api/prior-points/{bank}", json={
        "status": "resolved", "follow_up": "Saw signed reconciliations for March and June.",
        "management_response": "Done from January."}).json()
    assert out["points"][0]["checked_by"] == "Preparer"
    prep.patch(f"/api/prior-points/{duties}",
               json={"status": "not_resolved", "follow_up": "Same person still pays."})
    assert prep.post(f"/api/prior-points/{bank}/finding").status_code == 400
    r = prep.post(f"/api/prior-points/{duties}/finding")
    assert r.status_code == 200, r.text
    fid = r.json()["finding_id"]
    f = prep.get(f"/api/findings/{fid}").json()
    assert f["severity"] == "medium" and f["detail"].startswith("Segregate duties")
    assert prep.post(f"/api/prior-points/{duties}/finding").status_code == 409

    # Signing off: reviewers only, once followed up; a change takes it away.
    assert prep.patch(f"/api/prior-points/{bank}", json={"reviewed": True}).status_code == 403
    r = rev.patch(f"/api/prior-points/{assets}", json={"reviewed": True})
    assert r.status_code == 400
    out = rev.patch(f"/api/prior-points/{bank}", json={"reviewed": True}).json()
    assert out["points"][0]["reviewed_by"] == "Reviewer"
    out = prep.patch(f"/api/prior-points/{bank}", json={"management_response": "From Feb."})
    assert out.json()["points"][0]["reviewed_at"] is None
    assert section(prep, new, "prior")["needed"]

    # Next year brings what is still open again, but not the finding the point became.
    prep.patch(f"/api/prior-points/{assets}", json={"status": "partly",
                                                  "follow_up": "Updated to June only."})
    r = rev.post(f"/api/engagements/{new}/roll-forward", json={"name": "FY2028"})
    third = prep.get(f"/api/engagements/{r.json()['id']}/prior-points").json()["points"]
    assert [(p["origin"], p["title"], p["first_raised"]) for p in third] == [
        ("point", "Segregate duties over payments", "FY2026"),
        ("point", "Fixed asset register not updated", "FY2026")]
    assert third[1]["detail"] == "Last year: Updated to June only."

    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert list(book) == ["About", "Last year's points"] and len(book["Last year's points"]) == 3
    assert prep.get(f"/api/engagements/{new}/pack", params={"files": False}).status_code == 200
    acts = {x["action"] for x in a.get("/api/activity").json()}
    assert {"prior.carry", "prior.update", "prior.finding", "prior.download"} <= acts


def test_points_can_be_brought_in_added_by_hand_and_locked():
    a, prep, rev, partner, eid, _ = team()
    last_year(a, prep, rev, eid)
    client = a.get(f"/api/engagements/{eid}").json()["client"]["id"]
    new = a.post(f"/api/clients/{client}/engagements",
                 json={"name": "FY2027", "period_end": "2027-12-31"}).json()["id"]
    join(a, new, prep, rev, partner)
    url = f"/api/engagements/{new}/prior-points"
    assert prep.get(url).json()["points"] == []
    r = prep.post(f"{url}/import", json={"from_engagement_id": eid})
    assert r.status_code == 200 and r.json()["added"] == 3
    assert prep.post(f"{url}/import", json={"from_engagement_id": eid}).json()["added"] == 0
    # Only an earlier engagement of the same client.
    assert prep.post(f"/api/engagements/{eid}/prior-points/import",
                     json={"from_engagement_id": new}).status_code == 400
    other = a.post("/api/clients", json={"name": "Other"}).json()["id"]
    oe = a.post(f"/api/clients/{other}/engagements",
                json={"name": "FY2025", "period_end": "2025-12-31"}).json()["id"]
    assert prep.post(f"{url}/import", json={"from_engagement_id": oe}).status_code == 404
    assert a.post(f"{url}/import", json={"from_engagement_id": oe}).status_code == 400

    r = prep.post(url, json={"title": "Stock count instructions missing", "priority": "low"})
    assert r.status_code == 201 and r.json()["points"][-1]["origin"] == "manual"
    pid = r.json()["points"][-1]["id"]
    assert prep.post(url, json={"title": ""}).status_code == 422
    assert prep.delete(f"/api/prior-points/{pid}").status_code == 200

    assert partner.post(f"/api/engagements/{new}/lock", json={}).status_code == 200
    first = prep.get(url).json()["points"][0]["id"]
    assert prep.patch(f"/api/prior-points/{first}", json={"title": "x"}).status_code == 409
    assert prep.post(url, json={"title": "x"}).status_code == 409
    assert prep.get(f"{url}/export").status_code == 200


def bell(c):
    r = c.get("/api/notifications")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_bell_shows_what_needs_each_person():
    a, prep, rev, partner, eid, _ = team()
    outsider = add_user(a, "preparer", "outsider@example.com")
    assert bell(prep)["items"] == []
    r = rev.post(f"/api/engagements/{eid}/review-notes",
                 json={"section": "data", "text": "Why was this file used?"})
    nid = r.json()["id"]
    mine = bell(prep)
    assert [(x["kind"], x["text"], x["engagement"]) for x in mine["items"]] == [
        ("note", "Review note on Files and checks to answer", "Acme · FY2026")]
    assert mine["new"] == 1 and mine["items"][0]["panel"] == "rv-panel"
    assert bell(rev)["items"] == []   # their own question
    assert bell(outsider)["items"] == []   # not on the team

    me = prep.get("/api/auth/me").json()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    r = rev.post(f"/api/engagements/{eid}/tasks", json={
        "title": "Test cut-off", "assignee_id": me["id"], "due_on": yesterday})
    assert r.status_code == 200, r.text
    for title, days in (("Later", 30), ("Soon", 3)):
        rev.post(f"/api/engagements/{eid}/tasks", json={
            "title": title, "assignee_id": me["id"],
            "due_on": (date.today() + timedelta(days=days)).isoformat()})
    items = bell(prep)["items"]
    assert items[0]["kind"] == "task" and items[0]["urgent"] and items[0]["title"] == "Test cut-off"
    assert sorted(x.get("title", "") for x in items) == ["", "Soon", "Test cut-off"]

    # Opening the bell marks what is there as seen (a task due soon too); later items are new.
    assert prep.post("/api/notifications/seen").status_code == 200
    assert bell(prep)["new"] == 0
    assert prep.get("/api/auth/me").json()["language"] is None  # still not picked
    prep.post(f"/api/review-notes/{nid}/replies", json={"text": "It is the final ledger."})
    answered = bell(rev)["items"]
    assert [x["kind"] for x in answered] == ["note_answered"] and bell(rev)["new"] == 1
    assert [x["kind"] for x in bell(prep)["items"]] == ["task", "task"]

    # Files a client sent, and, for admins, people asking for an account.
    with db.SessionLocal() as s:
        req = db.DocumentRequest(engagement_id=eid, number=1, title="Bank letters",
                                 status="submitted")
        s.add(req)
        s.commit()
    assert any(x["kind"] == "request" and x["title"] == "1. Bank letters"
               for x in bell(prep)["items"])
    TestClient(app).post("/api/auth/signup", json={
        "email": "new@example.com", "name": "New Person", "password": "long enough pw 1"})
    assert any(x["kind"] == "signup" and x["count"] == 1 for x in bell(a)["items"])
    assert not any(x["kind"] == "signup" for x in bell(partner)["items"])

    # A locked file needs nothing more.
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert bell(prep)["items"] == []
