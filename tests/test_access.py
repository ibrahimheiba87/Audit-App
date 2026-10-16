"""Per-engagement access: only admins see engagements they aren't assigned to."""

import pytest

from app import assistant, auth, db
from app.main import app
from tests.test_auth import add_user, admin, join, setup_engagement
from tests.test_portal import PDF, add_client_user

pytestmark = pytest.mark.usefixtures("real_auth")

TB = ("tb.csv", b"Account,Description,Amount\n1000,Cash,9\n")
PRIOR = ("prior.csv", b"Account,Description,Amount\n1000,Cash,5\n")
AR = ("ar.csv", b"Customer,Invoice,Invoice date,Amount\nC1,I1,2026-01-05,100\n")
BANK = ("bank.csv", b"Date,Description,Withdrawals,Deposits\n2026-05-21,CHQ,33.32,\n")
REC = {"accounts": ["1000 Cash"]}


def full_engagement(a):
    """An engagement with one of everything, and the ids of each."""
    eid, gl = setup_engagement(a)
    a.put(f"/api/engagements/{eid}/materiality",
          json={"benchmark": "revenue", "benchmark_amount": 1_000_000})
    tb = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "tb"},
                files={"file": TB}).json()["id"]
    prior = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "prior_tb"},
                   files={"file": PRIOR}).json()["id"]
    run = a.post(f"/api/engagements/{eid}/runs", json={"upload_id": gl}).json()["id"]
    finding = a.post(f"/api/engagements/{eid}/findings", json={"detail": "x"}).json()["id"]
    evidence = a.post(f"/api/findings/{finding}/evidence",
                      files={"file": ("a.txt", b"abc")}).json()["attachments"][0]["id"]
    comparison = a.post(f"/api/engagements/{eid}/comparisons",
                        json={"current_upload_id": tb, "prior_upload_id": prior}).json()["id"]
    sample = a.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": gl, "method": "random", "sample_size": 2}).json()["id"]
    entry = a.post(f"/api/engagements/{eid}/adjusting-entries", json={
        "kind": "proposed", "description": "Cut-off",
        "lines": [{"account": "4000", "debit": 10}, {"account": "1200", "credit": 10}]}).json()
    a.post(f"/api/engagements/{eid}/signoffs", json={"section": "data", "kind": "prepared"})
    note = a.post(f"/api/engagements/{eid}/review-notes",
                  json={"section": "data", "text": "Check this"}).json()["id"]
    bank = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "bank"},
                  files={"file": BANK}).json()["id"]
    rec = a.post(f"/api/engagements/{eid}/bank-reconciliations",
                 json={**REC, "bank_upload_id": bank, "gl_upload_id": gl}).json()["id"]
    req = a.post(f"/api/engagements/{eid}/requests", json={"title": "Bank"}).json()["id"]
    file = a.post(f"/api/requests/{req}/files", files={"file": PDF}).json()["files"][0]["id"]
    with db.SessionLocal() as s:
        draft = db.AiDraft(engagement_id=eid, kind="letter", content={
            "intro": "", "closing": "", "points": []}, created_by="A")
        risk_draft = db.AiDraft(engagement_id=eid, kind="risk", content={
            "summary": "", "risks": []}, created_by="A")
        s.add_all([draft, risk_draft])
        s.commit()
        draft_id, risk_draft_id = draft.id, risk_draft.id
    risk = a.post(f"/api/engagements/{eid}/risks", json={
        "area": "revenue", "title": "Cut-off", "level": "significant",
        "procedures": ["Test cut-off"]}).json()
    ar = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "ar"},
                files={"file": AR}).json()["id"]
    match = a.post(f"/api/engagements/{eid}/cross-matches", json={
        "left_upload_id": ar, "right_upload_id": ar,
        "keys": [{"left": "Customer", "right": "Customer"}]})
    assert match.status_code == 201, match.text
    confirmation = a.post(f"/api/engagements/{eid}/confirmations", json={
        "kind": "bank", "party": "First Bank", "balance": 100})
    assert confirmation.status_code == 201, confirmation.text
    control = a.post(f"/api/engagements/{eid}/controls", json={
        "area": "revenue", "title": "Credit notes approved"})
    assert control.status_code == 201, control.text
    a.post(f"/api/controls/{control.json()['id']}/plan", json={"method": "frequency"})
    return {"engagement_id": eid, "upload_id": gl, "run_id": run, "finding_id": finding,
            "evidence_id": evidence, "comparison_id": comparison, "sample_id": sample,
            "entry_id": entry["id"], "note_id": note, "request_id": req, "file_id": file,
            "section": "data", "kind": "prepared", "bank_upload_id": bank, "rec_id": rec,
            "draft_id": draft_id, "risk_draft_id": risk_draft_id, "risk_id": risk["id"],
            "procedure_id": risk["procedures"][0]["id"], "ar_upload_id": ar,
            "match_id": match.json()["id"], "confirmation_id": confirmation.json()["id"],
            "control_id": control.json()["id"]}


def bodies(ids):
    """A valid request for every route that belongs to an engagement, so a 404 can only come
    from the access check (not from validation)."""
    entry = {"kind": "proposed", "description": "x",
             "lines": [{"account": "4000", "debit": 1}, {"account": "1200", "credit": 1}]}
    return {
        ("GET", "/api/engagements/{engagement_id}"): {},
        ("PATCH", "/api/engagements/{engagement_id}"): {"json": {"name": "x"}},
        ("DELETE", "/api/engagements/{engagement_id}"): {},
        ("PUT", "/api/engagements/{engagement_id}/materiality"):
            {"json": {"benchmark": "revenue", "benchmark_amount": 1000}},
        ("POST", "/api/engagements/{engagement_id}/uploads"):
            {"data": {"kind": "tb"}, "files": {"file": TB}},
        ("DELETE", "/api/uploads/{upload_id}"): {},
        ("POST", "/api/engagements/{engagement_id}/runs"): {"json": {"upload_id": ids["upload_id"]}},
        ("GET", "/api/runs/{run_id}"): {},
        ("GET", "/api/runs/{run_id}/report"): {},
        ("POST", "/api/engagements/{engagement_id}/comparisons"):
            {"json": {"current_upload_id": 1, "prior_upload_id": 1}},
        ("GET", "/api/comparisons/{comparison_id}"): {},
        ("GET", "/api/comparisons/{comparison_id}/report"): {},
        ("POST", "/api/engagements/{engagement_id}/samples"):
            {"json": {"upload_id": ids["upload_id"], "method": "random", "sample_size": 1}},
        ("GET", "/api/samples/{sample_id}"): {},
        ("GET", "/api/samples/{sample_id}/export"): {},
        ("PUT", "/api/samples/{sample_id}/evaluation"): {"json": {"items": []}},
        ("POST", "/api/uploads/{upload_id}/stratify"): {"json": {}},
        ("GET", "/api/uploads/{upload_id}/stratify/export"): {},
        ("POST", "/api/uploads/{upload_id}/aging"): {"json": {}},
        ("GET", "/api/uploads/{upload_id}/aging/export"): {},
        ("GET", "/api/engagements/{engagement_id}/risks"): {},
        ("POST", "/api/engagements/{engagement_id}/risks"):
            {"json": {"area": "revenue", "title": "x", "level": "lower"}},
        ("POST", "/api/engagements/{engagement_id}/risks/from-draft/{draft_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/risks/export"): {},
        ("PATCH", "/api/risks/{risk_id}"): {"json": {"title": "y"}},
        ("DELETE", "/api/risks/{risk_id}"): {},
        ("POST", "/api/risks/{risk_id}/procedures"): {"json": {"text": "x"}},
        ("PATCH", "/api/procedures/{procedure_id}"): {"json": {"status": "done"}},
        ("DELETE", "/api/procedures/{procedure_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/confirmations"): {},
        ("POST", "/api/engagements/{engagement_id}/confirmations"):
            {"json": {"kind": "other", "party": "x"}},
        ("POST", "/api/engagements/{engagement_id}/confirmations/from-listing"):
            {"json": {"upload_id": ids["ar_upload_id"], "largest": 2}},
        ("GET", "/api/engagements/{engagement_id}/confirmations/export"): {},
        ("PATCH", "/api/confirmations/{confirmation_id}"): {"json": {"status": "sent"}},
        ("DELETE", "/api/confirmations/{confirmation_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/controls"): {},
        ("POST", "/api/engagements/{engagement_id}/controls"):
            {"json": {"area": "fraud", "title": "x"}},
        ("GET", "/api/engagements/{engagement_id}/controls/export"): {},
        ("PATCH", "/api/controls/{control_id}"): {"json": {"design": "effective"}},
        ("POST", "/api/controls/{control_id}/plan"): {"json": {"method": "frequency"}},
        ("POST", "/api/controls/{control_id}/result"): {"json": {"tested": 2, "deviations": 0}},
        ("DELETE", "/api/controls/{control_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/findings"): {},
        ("POST", "/api/engagements/{engagement_id}/findings"): {"json": {"detail": "x"}},
        ("POST", "/api/engagements/{engagement_id}/findings/bulk"):
            {"json": {"ids": [ids["finding_id"]], "status": "open"}},
        ("GET", "/api/engagements/{engagement_id}/findings/export"): {},
        ("GET", "/api/findings/{finding_id}"): {},
        ("PATCH", "/api/findings/{finding_id}"): {"json": {"status": "reviewed"}},
        ("DELETE", "/api/findings/{finding_id}"): {},
        ("POST", "/api/findings/{finding_id}/notes"): {"json": {"text": "x"}},
        ("POST", "/api/findings/{finding_id}/evidence"): {"files": {"file": ("b.txt", b"b")}},
        ("POST", "/api/findings/{finding_id}/assistant"): {"json": {"task": "explain"}},
        ("GET", "/api/evidence/{evidence_id}"): {},
        ("DELETE", "/api/evidence/{evidence_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/statements"): {},
        ("GET", "/api/engagements/{engagement_id}/statements/export"): {},
        ("PUT", "/api/engagements/{engagement_id}/mapping"): {"json": {"mappings": {}}},
        ("POST", "/api/engagements/{engagement_id}/adjusting-entries"): {"json": entry},
        ("PATCH", "/api/adjusting-entries/{entry_id}"): {"json": {"kind": "adjusting"}},
        ("DELETE", "/api/adjusting-entries/{entry_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/review"): {},
        ("POST", "/api/engagements/{engagement_id}/signoffs"):
            {"json": {"section": "sampling", "kind": "prepared"}},
        ("DELETE", "/api/engagements/{engagement_id}/signoffs/{section}/{kind}"): {},
        ("POST", "/api/engagements/{engagement_id}/review-notes"):
            {"json": {"section": "data", "text": "x"}},
        ("POST", "/api/review-notes/{note_id}/replies"): {"json": {"text": "x"}},
        ("PATCH", "/api/review-notes/{note_id}"): {"json": {"status": "cleared"}},
        ("DELETE", "/api/review-notes/{note_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/requests"): {},
        ("GET", "/api/engagements/{engagement_id}/dashboard"): {},
        ("GET", "/api/engagements/{engagement_id}/bank-reconciliations/options"): {},
        ("POST", "/api/engagements/{engagement_id}/bank-reconciliations"):
            {"json": {**REC, "bank_upload_id": ids["bank_upload_id"],
                      "gl_upload_id": ids["upload_id"]}},
        ("GET", "/api/bank-reconciliations/{rec_id}"): {},
        ("GET", "/api/bank-reconciliations/{rec_id}/report"): {},
        ("POST", "/api/engagements/{engagement_id}/roll-forward"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/requests"): {"json": {"title": "x"}},
        ("GET", "/api/engagements/{engagement_id}/requests/export"): {},
        ("PATCH", "/api/requests/{request_id}"): {"json": {"title": "y"}},
        ("POST", "/api/requests/{request_id}/status"): {"json": {"status": "accepted"}},
        ("DELETE", "/api/requests/{request_id}"): {},
        ("POST", "/api/engagements/{engagement_id}/mapping/suggest"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/documents/read"):
            {"data": {"kind": "invoice"}, "files": {"file": PDF}},
        ("POST", "/api/engagements/{engagement_id}/ai/risk-assessment"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/ai/management-letter"): {"json": {}},
        ("GET", "/api/engagements/{engagement_id}/ai-drafts"): {},
        ("GET", "/api/ai-drafts/{draft_id}"): {},
        ("GET", "/api/ai-drafts/{draft_id}/export"): {},
        ("POST", "/api/requests/{request_id}/files"): {"files": {"file": PDF}},
        ("POST", "/api/requests/{request_id}/messages"): {"json": {"text": "x"}},
        ("GET", "/api/request-files/{file_id}"): {},
        ("DELETE", "/api/request-files/{file_id}"): {},
        ("POST", "/api/engagements/{engagement_id}/lock"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/archive"): {"json": {"archived": True}},
        ("GET", "/api/engagements/{engagement_id}/acceptance"): {},
        ("PUT", "/api/engagements/{engagement_id}/acceptance"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/acceptance/decision"):
            {"json": {"decision": "decline"}},
        ("GET", "/api/engagements/{engagement_id}/acceptance/export"): {},
        ("POST", "/api/engagements/{engagement_id}/independence"): {"json": {"independent": True}},
        ("GET", "/api/uploads/{upload_id}/columns"): {},
        ("POST", "/api/engagements/{engagement_id}/cross-matches"): {"json": {
            "left_upload_id": ids["ar_upload_id"], "right_upload_id": ids["ar_upload_id"],
            "keys": [{"left": "Customer", "right": "Customer"}]}},
        ("GET", "/api/cross-matches/{match_id}"): {},
        ("GET", "/api/cross-matches/{match_id}/export"): {},
        ("DELETE", "/api/cross-matches/{match_id}"): {},
        ("GET", "/api/engagements/{engagement_id}/disclosures"): {},
        ("PUT", "/api/engagements/{engagement_id}/disclosures"): {"json": {}},
        ("POST", "/api/engagements/{engagement_id}/disclosures/ai-review"):
            {"files": {"file": PDF}},
        ("POST", "/api/engagements/{engagement_id}/disclosures/accept-ai"): {},
        ("GET", "/api/engagements/{engagement_id}/disclosures/export"): {},
        ("GET", "/api/engagements/{engagement_id}/pack"): {"params": {"files": "true"}},
    }


# Routes with ids that aren't an engagement's records, tested on their own below.
NOT_ENGAGEMENT_ROUTES = {
    ("GET", "/api/clients/{client_id}"), ("PATCH", "/api/clients/{client_id}"),
    ("DELETE", "/api/clients/{client_id}"), ("POST", "/api/clients/{client_id}/engagements"),
    ("PATCH", "/api/users/{user_id}"),
    ("PUT", "/api/engagements/{engagement_id}/team"),  # admins only
    ("DELETE", "/api/engagements/{engagement_id}/lock"),  # admins only
    ("PUT", "/api/test-templates/{template_id}"),  # firm-wide, not an engagement's
    ("DELETE", "/api/test-templates/{template_id}"),
}


def test_every_engagement_route_looks_missing_to_someone_not_on_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(assistant, "call_claude", lambda system, prompt: "answer")
    monkeypatch.setattr(assistant, "request_claude", lambda system, content, **kw: "{}")
    a = admin()
    ids = full_engagement(a)
    table = bodies(ids)
    routes = {(method.upper(), path) for path, ops in app.openapi()["paths"].items()
              for method in ops if "{" in path}
    assert routes - NOT_ENGAGEMENT_ROUTES == set(table), \
        "add new routes with ids to bodies() or NOT_ENGAGEMENT_ROUTES"
    # A partner can do anything but admin work, so a refusal can only come from the team check.
    outsider = add_user(a, "partner")
    held, hold = [], auth.hold
    monkeypatch.setattr(auth, "hold", lambda s, key: (held.append(key), hold(s, key)))
    for (method, path), kwargs in table.items():
        url = path.format(**ids)
        r = outsider.request(method, url, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        # Holding the engagement's guards, even briefly, would hold up the team's work.
        assert not held, (method, path, held)
    # Nothing was changed, and the same requests work once they are on the team.
    with db.SessionLocal() as s:
        assert s.get(db.Engagement, ids["engagement_id"]).name == "FY2026"
    join(a, ids["engagement_id"], outsider)
    for method, path in [("GET", "/api/engagements/{engagement_id}"),
                         ("GET", "/api/findings/{finding_id}"),
                         ("GET", "/api/request-files/{file_id}"),
                         ("GET", "/api/evidence/{evidence_id}"),
                         ("POST", "/api/findings/{finding_id}/assistant")]:
        r = outsider.request(method, path.format(**ids), **table[(method, path)])
        assert r.status_code == 200, (method, path, r.text)


def test_lists_show_only_assigned_engagements():
    a = admin()
    prep = add_user(a, "preparer")
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    one = a.post(f"/api/clients/{acme['id']}/engagements",
                 json={"name": "FY2025", "period_end": "2025-12-31"}).json()["id"]
    two = a.post(f"/api/clients/{acme['id']}/engagements",
                 json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    a.post("/api/clients", json={"name": "Newco"})  # no engagements yet
    assert [c["name"] for c in prep.get("/api/clients").json()] == ["Newco"]
    assert prep.get(f"/api/clients/{acme['id']}").status_code == 404
    assert prep.get("/api/activity", params={"engagement_id": one}).status_code == 404
    join(a, two, prep)
    [client, newco] = prep.get("/api/clients").json()
    assert [e["id"] for e in client["engagements"]] == [two] and newco["engagements"] == []
    assert [e["id"] for e in prep.get(f"/api/clients/{acme['id']}").json()["engagements"]] == [two]
    assert prep.get("/api/activity", params={"engagement_id": two}).status_code == 200
    # Admins see everything.
    assert len(a.get("/api/clients").json()[0]["engagements"]) == 2


def test_team_changes_are_for_admins_and_logged():
    a = admin()
    prep, partner = add_user(a, "preparer"), add_user(a, "partner")
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    cl = add_client_user(a, acme["id"])
    eid = a.post(f"/api/clients/{acme['id']}/engagements",
                 json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    pid = prep.get("/api/auth/me").json()["id"]
    join(a, eid, partner)
    assert partner.put(f"/api/engagements/{eid}/team", json={"user_ids": [pid]}).status_code == 403
    r = a.put(f"/api/engagements/{eid}/team",
              json={"user_ids": [cl.get("/api/auth/me").json()["id"]]})
    assert r.status_code == 400 and "Client users" in r.json()["detail"]
    assert a.put(f"/api/engagements/{eid}/team", json={"user_ids": [999]}).status_code == 400
    r = a.put(f"/api/engagements/{eid}/team", json={"user_ids": [pid]})
    assert [m["name"] for m in r.json()["team"]] == ["Preparer"]
    assert partner.get(f"/api/engagements/{eid}").status_code == 404  # taken off
    log = a.get("/api/activity", params={"action": "engagement.team"}).json()[0]
    assert log["summary"] == "Changed the team: added Preparer; removed Partner"
    assert log["details"] == {"added": [pid], "removed": [partner.get("/api/auth/me").json()["id"]]}
    # Saving the same team again changes nothing and logs nothing.
    a.put(f"/api/engagements/{eid}/team", json={"user_ids": [pid]})
    assert len(a.get("/api/activity", params={"action": "engagement.team"}).json()) == 2


def test_admins_are_not_added_to_teams_but_keep_a_place_they_had():
    a = admin()
    prep, other = add_user(a, "preparer"), add_user(a, "preparer", "other@example.com")
    eid, _ = setup_engagement(a)
    join(a, eid, prep, other)
    pid = prep.get("/api/auth/me").json()["id"]
    oid = other.get("/api/auth/me").json()["id"]
    r = a.put(f"/api/engagements/{eid}/team",
              json={"user_ids": [oid, a.get("/api/auth/me").json()["id"]]})
    assert r.status_code == 400 and "Admins" in r.json()["detail"]
    # Someone on the team who becomes an admin stays on it when the team is saved without them
    # (the team dialog can't untick admins), so they keep it if they stop being an admin.
    a.patch(f"/api/users/{pid}", json={"role": "admin"})
    r = a.put(f"/api/engagements/{eid}/team", json={"user_ids": []})
    assert [m["id"] for m in r.json()["team"]] == [pid]
    assert a.get("/api/activity", params={"action": "engagement.team"}).json()[0]["details"] \
        == {"added": [], "removed": [oid]}
    a.patch(f"/api/users/{pid}", json={"role": "preparer"})
    assert prep.get(f"/api/engagements/{eid}").status_code == 200


def test_whoever_creates_an_engagement_is_on_its_team():
    a = admin()
    rev = add_user(a, "reviewer")
    eid, _ = setup_engagement(rev)
    assert [m["name"] for m in rev.get(f"/api/engagements/{eid}").json()["team"]] == ["Reviewer"]
    other = a.post("/api/clients", json={"name": "Globex"}).json()  # admins needn't be on it
    assert a.post(f"/api/clients/{other['id']}/engagements",
                  json={"name": "FY2026", "period_end": "2026-12-31"}).status_code == 201
    with db.SessionLocal() as s:
        assert s.query(db.EngagementMember).count() == 1


def test_becoming_a_client_user_takes_someone_off_every_team():
    a = admin()
    prep = add_user(a, "preparer")
    eid, _ = setup_engagement(a)
    join(a, eid, prep)
    acme = a.get("/api/clients").json()[0]
    pid = prep.get("/api/auth/me").json()["id"]
    a.patch(f"/api/users/{pid}", json={"role": "client", "client_id": acme["id"]})
    a.patch(f"/api/users/{pid}", json={"role": "preparer"})
    assert a.get(f"/api/engagements/{eid}").json()["team"] == []
    log = a.get("/api/activity", params={"action": "user.update"}).json()[1]
    assert "taken off 1 engagements" in log["summary"]


def test_deleting_a_client_needs_every_engagement(monkeypatch):
    a = admin()
    partner = add_user(a, "partner")
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    ids = [a.post(f"/api/clients/{acme['id']}/engagements",
                  json={"name": n, "period_end": "2026-12-31"}).json()["id"] for n in "AB"]
    join(a, ids[0], partner)
    held, hold = [], auth.hold
    monkeypatch.setattr(auth, "hold", lambda s, key: (held.append(key), hold(s, key)))
    r = partner.delete(f"/api/clients/{acme['id']}")
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    assert not held  # nothing of the engagement they aren't on was held up
    join(a, ids[1], partner)
    assert partner.delete(f"/api/clients/{acme['id']}").status_code == 204


def test_deleting_an_engagement_removes_its_team():
    a = admin()
    prep = add_user(a, "preparer")
    eid, _ = setup_engagement(a)
    join(a, eid, prep)
    assert a.delete(f"/api/engagements/{eid}").status_code == 204
    with db.SessionLocal() as s:
        assert s.query(db.EngagementMember).count() == 0
