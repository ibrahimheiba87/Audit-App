"""Review notes and sign-off by section."""

import pytest

from app import db, review
from tests.test_auth import add_user, admin, setup_engagement

pytestmark = pytest.mark.usefixtures("real_auth")


def team():
    a = admin()
    prep, rev, partner = (add_user(a, r) for r in ("preparer", "reviewer", "partner"))
    eid, uid = setup_engagement(a)
    return a, prep, rev, partner, eid, uid


def sign(c, eid, section, kind):
    return c.post(f"/api/engagements/{eid}/signoffs", json={"section": section, "kind": kind})


def section(c, eid, sid):
    data = c.get(f"/api/engagements/{eid}/review").json()
    return next(s for s in data["sections"] if s["id"] == sid)


def test_prepare_then_review_by_someone_else():
    _, prep, rev, _, eid, _ = team()
    assert section(prep, eid, "data")["status"] == "Not started"
    r = sign(rev, eid, "data", "reviewed")
    assert r.status_code == 400 and "prepared" in r.json()["detail"]
    assert sign(prep, eid, "data", "prepared").status_code == 201
    assert section(prep, eid, "data")["status"] == "Ready for review"
    assert sign(prep, eid, "data", "reviewed").status_code == 403  # preparers can't review
    assert sign(rev, eid, "data", "reviewed").status_code == 201
    s = section(prep, eid, "data")
    assert s["status"] == "Reviewed" and s["done"] and s["reviewed"]["user_name"] == "Reviewer"
    log = prep.get("/api/activity", params={"engagement_id": eid}).json()
    assert [x["action"] for x in log[:2]] == ["signoff.add", "signoff.add"]


def test_reviewer_cannot_review_their_own_preparation():
    _, _, rev, _, eid, _ = team()
    sign(rev, eid, "data", "prepared")
    r = sign(rev, eid, "data", "reviewed")
    assert r.status_code == 400 and "someone else" in r.json()["detail"]


def test_changes_after_sign_off_make_it_stale():
    _, prep, rev, _, eid, uid = team()
    sign(prep, eid, "data", "prepared")
    sign(rev, eid, "data", "reviewed")
    assert prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid}).status_code == 201
    s = section(prep, eid, "data")
    assert s["status"] == "Changed since prepared"
    assert s["prepared"]["stale"] and s["reviewed"]["stale"]
    assert section(prep, eid, "sampling")["status"] == "Not started"  # other sections unmoved
    r = sign(rev, eid, "data", "reviewed")
    assert r.status_code == 400 and "again" in r.json()["detail"]
    sign(prep, eid, "data", "prepared")
    assert section(prep, eid, "data")["status"] == "Changed since review"
    sign(rev, eid, "data", "reviewed")
    assert section(prep, eid, "data")["status"] == "Reviewed"


def test_review_notes_block_review_until_cleared():
    _, prep, rev, _, eid, _ = team()
    sign(prep, eid, "data", "prepared")
    sign(rev, eid, "data", "reviewed")
    assert prep.post(f"/api/engagements/{eid}/review-notes",
                     json={"section": "data", "text": "Why?"}).status_code == 403
    note = rev.post(f"/api/engagements/{eid}/review-notes",
                    json={"section": "data", "text": "Why was the GL re-uploaded?"}).json()
    assert note["status"] == "open"
    s = section(prep, eid, "data")
    assert s["reviewed"] is None and s["open_notes"] == 1  # raising a note withdraws the review
    r = sign(rev, eid, "data", "reviewed")
    assert r.status_code == 400 and "1 review note" in r.json()["detail"]
    assert rev.patch(f"/api/review-notes/{note['id']}", json={"status": "cleared"}).json()
    reply = prep.post(f"/api/review-notes/{note['id']}/replies", json={"text": "Late JEs"})
    assert reply.status_code == 400  # cleared notes need reopening first
    rev.patch(f"/api/review-notes/{note['id']}", json={"status": "open"})
    reply = prep.post(f"/api/review-notes/{note['id']}/replies", json={"text": "Late JEs"})
    assert reply.json()["status"] == "responded" and reply.json()["replies"][0]["author"] == \
        "Preparer"
    assert prep.patch(f"/api/review-notes/{note['id']}",
                      json={"status": "cleared"}).status_code == 403
    cleared = rev.patch(f"/api/review-notes/{note['id']}", json={"status": "cleared"}).json()
    assert cleared["status"] == "cleared" and cleared["cleared_by"] == "Reviewer"
    assert sign(rev, eid, "data", "reviewed").status_code == 201
    actions = [x["action"] for x in prep.get("/api/activity",
                                             params={"engagement_id": eid}).json()]
    for a in ("note.create", "note.reply", "note.clear", "note.reopen"):
        assert a in actions


def test_only_the_author_deletes_a_note_before_replies():
    _, prep, rev, partner, eid, _ = team()
    n1 = rev.post(f"/api/engagements/{eid}/review-notes",
                  json={"section": "findings", "text": "Typo"}).json()
    assert partner.delete(f"/api/review-notes/{n1['id']}").status_code == 400
    assert rev.delete(f"/api/review-notes/{n1['id']}").status_code == 204
    n2 = rev.post(f"/api/engagements/{eid}/review-notes",
                  json={"section": "findings", "text": "Support?"}).json()
    prep.post(f"/api/review-notes/{n2['id']}/replies", json={"text": "Attached"})
    assert rev.delete(f"/api/review-notes/{n2['id']}").status_code == 400


def test_completion_needs_every_section_reviewed_and_a_partner():
    _, prep, rev, partner, eid, _ = team()
    r = sign(prep, eid, "completion", "prepared")
    assert r.status_code == 400 and "Still to do" in r.json()["detail"]
    for s in review.WORK:
        sign(prep, eid, s.id, "prepared")
    assert sign(prep, eid, "completion", "prepared").status_code == 201
    r = sign(partner, eid, "completion", "reviewed")
    assert r.status_code == 400 and "Files and checks" in r.json()["detail"]
    for s in review.WORK:
        assert sign(rev, eid, s.id, "reviewed").status_code == 201
    assert sign(rev, eid, "completion", "reviewed").status_code == 403  # partners sign it
    assert sign(partner, eid, "completion", "reviewed").status_code == 201
    data = prep.get(f"/api/engagements/{eid}/review").json()
    assert data["reviewed"] == data["total"] == len(review.SECTIONS)
    rev.post(f"/api/engagements/{eid}/review-notes", json={"section": "sampling", "text": "?"})
    assert section(prep, eid, "completion")["reviewed"] is None


def test_taking_back_sign_offs():
    _, prep, rev, partner, eid, _ = team()
    other = add_user(admin(), "preparer", "other@example.com")
    sign(prep, eid, "data", "prepared")
    sign(rev, eid, "data", "reviewed")
    assert other.delete(f"/api/engagements/{eid}/signoffs/data/prepared").status_code == 403
    r = prep.delete(f"/api/engagements/{eid}/signoffs/data/prepared")
    assert r.status_code == 200  # your own, and the review of it goes too
    s = next(x for x in r.json()["sections"] if x["id"] == "data")
    assert s["prepared"] is None and s["reviewed"] is None
    assert prep.delete(f"/api/engagements/{eid}/signoffs/data/prepared").status_code == 404
    log = prep.get("/api/activity", params={"engagement_id": eid}).json()[0]
    assert log["action"] == "signoff.remove" and log["details"]["kinds"] == ["prepared",
                                                                             "reviewed"]


def test_deleting_an_engagement_removes_its_review():
    a, prep, rev, partner, eid, _ = team()
    sign(prep, eid, "data", "prepared")
    n = rev.post(f"/api/engagements/{eid}/review-notes",
                 json={"section": "data", "text": "?"}).json()
    prep.post(f"/api/review-notes/{n['id']}/replies", json={"text": "!"})
    assert partner.delete(f"/api/engagements/{eid}").status_code == 204
    with db.SessionLocal() as s:
        assert not s.query(db.SignOff).count() and not s.query(db.ReviewNote).count()
        assert not s.query(db.ReviewReply).count()


def test_bad_input():
    _, prep, rev, _, eid, _ = team()
    assert sign(prep, eid, "nope", "prepared").status_code == 422
    assert sign(prep, eid, "data", "approved").status_code == 422
    assert rev.post(f"/api/engagements/{eid}/review-notes",
                    json={"section": "data", "text": "   "}).status_code == 422
    assert prep.get("/api/engagements/9999/review").status_code == 404


def test_concurrent_sign_offs_do_not_fail(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    _, prep, _, _, eid, _ = team()
    real = review.log

    def slow_log(*args, **kwargs):  # widen the window between reading and committing
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(review, "log", slow_log)
    barrier = threading.Barrier(2)

    def run(_):
        barrier.wait()
        return sign(prep, eid, "data", "prepared").status_code

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, range(2))) == [201, 201]
    with db.SessionLocal() as s:
        assert s.query(db.SignOff).count() == 1
