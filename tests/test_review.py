"""Review notes and sign-off by section."""

import pytest

from app import db, review
from tests.test_auth import GL, add_user, admin, join, setup_engagement

TB = GL.parent / "sample_tb_2026.csv"

pytestmark = pytest.mark.usefixtures("real_auth")


def team():
    a = admin()
    prep, rev, partner = (add_user(a, r) for r in ("preparer", "reviewer", "partner"))
    eid, uid = setup_engagement(a)
    join(a, eid, prep, rev, partner)
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
    done = section(prep, eid, "completion")
    assert done["reviewed"]["stale"] and done["status"] == "Changed since review"


def test_taking_back_sign_offs():
    _, prep, rev, partner, eid, _ = team()
    other = add_user(admin(), "preparer", "other@example.com")
    join(admin(), eid, other)
    sign(prep, eid, "data", "prepared")
    sign(rev, eid, "data", "reviewed")
    assert other.delete(f"/api/engagements/{eid}/signoffs/data/prepared").status_code == 403
    assert prep.delete(f"/api/engagements/{eid}/signoffs/data/reviewed").status_code == 403
    r = prep.delete(f"/api/engagements/{eid}/signoffs/data/prepared")
    assert r.status_code == 200  # your own; the review of it then shows as changed
    s = next(x for x in r.json()["sections"] if x["id"] == "data")
    assert s["prepared"] is None and s["reviewed"]["stale"] and s["status"] == "Not started"
    assert prep.delete(f"/api/engagements/{eid}/signoffs/data/prepared").status_code == 404
    log = prep.get("/api/activity", params={"engagement_id": eid}).json()[0]
    assert log["action"] == "signoff.remove" and log["details"]["kinds"] == ["prepared"]


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


# --- Review fixes ---------------------------------------------------------------


def test_side_effects_make_other_sections_stale():
    a, prep, rev, partner, eid, uid = team()
    for sid in ("findings", "sampling"):
        sign(prep, eid, sid, "prepared")
        sign(rev, eid, sid, "reviewed")
    prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})  # adds findings
    assert section(prep, eid, "findings")["status"] == "Changed since prepared"
    assert section(prep, eid, "sampling")["status"] == "Reviewed"
    for sid in ("comparison", "statements"):
        sign(prep, eid, sid, "prepared")
    assert rev.delete(f"/api/uploads/{uid}").status_code == 204  # its findings go too
    assert section(prep, eid, "findings")["status"] == "Changed since prepared"
    # The file had no samples or comparisons, and a ledger doesn't feed the statements.
    for sid in ("sampling", "comparison", "statements"):
        assert not section(prep, eid, sid)["prepared"]["stale"], sid


def test_deleting_a_file_makes_its_samples_stale():
    a, prep, rev, partner, eid, uid = team()
    r = prep.post(f"/api/engagements/{eid}/samples",
                  json={"upload_id": uid, "method": "random", "sample_size": 3, "seed": 1})
    assert r.status_code == 201, r.text
    sign(prep, eid, "sampling", "prepared")
    assert rev.delete(f"/api/uploads/{uid}").status_code == 204
    assert section(prep, eid, "sampling")["status"] == "Changed since prepared"


def test_only_trial_balances_make_the_statements_stale():
    a, prep, rev, partner, eid, _ = team()
    sign(prep, eid, "statements", "prepared")
    gl = GL.read_bytes()
    prep.post(f"/api/engagements/{eid}/uploads", files={"file": ("gl2.csv", gl, "text/csv")})
    assert not section(prep, eid, "statements")["prepared"]["stale"]
    prep.post(f"/api/engagements/{eid}/uploads", data={"kind": "tb"},
              files={"file": ("tb.csv", TB.read_bytes(), "text/csv")})
    assert section(prep, eid, "statements")["prepared"]["stale"]


def test_completion_is_stale_after_a_section_is_reviewed_again():
    _, prep, rev, partner, eid, _ = team()
    for s in review.WORK:
        sign(prep, eid, s.id, "prepared")
        sign(rev, eid, s.id, "reviewed")
    sign(prep, eid, "completion", "prepared")
    sign(partner, eid, "completion", "reviewed")
    note = partner.post(f"/api/engagements/{eid}/review-notes",
                        json={"section": "data", "text": "Support?"}).json()
    partner.patch(f"/api/review-notes/{note['id']}", json={"status": "cleared"})
    sign(rev, eid, "data", "reviewed")  # back to done, but after completion was signed
    done = section(prep, eid, "completion")
    assert done["reviewed"]["stale"] and not done["done"]
    assert sign(partner, eid, "completion", "reviewed").status_code == 201
    assert section(prep, eid, "completion")["done"]


def test_completion_follows_the_sections_it_stands_on():
    _, prep, rev, partner, eid, _ = team()
    for s in review.WORK:
        sign(prep, eid, s.id, "prepared")
        sign(rev, eid, s.id, "reviewed")
    sign(prep, eid, "completion", "prepared")
    sign(partner, eid, "completion", "reviewed")
    assert section(prep, eid, "completion")["done"]
    rev.delete(f"/api/engagements/{eid}/signoffs/data/reviewed")
    done = section(prep, eid, "completion")
    assert not done["done"] and done["reviewed"]["stale"] and not done["prepared"]["stale"]
    prep.delete(f"/api/engagements/{eid}/signoffs/sampling/prepared")
    assert section(prep, eid, "completion")["prepared"]["stale"]


def test_a_current_preparation_cannot_be_signed_over():
    _, prep, rev, _, eid, _ = team()
    sign(rev, eid, "data", "prepared")
    r = sign(prep, eid, "data", "prepared")
    assert r.status_code == 400 and "already signed off" in r.json()["detail"]
    assert sign(rev, eid, "data", "prepared").status_code == 201  # re-signing your own is fine


def test_the_preparer_cannot_clear_notes_on_their_own_work():
    a, prep, rev, partner, eid, _ = team()
    sign(rev, eid, "data", "prepared")
    note = partner.post(f"/api/engagements/{eid}/review-notes",
                        json={"section": "data", "text": "Support?"}).json()
    r = rev.patch(f"/api/review-notes/{note['id']}", json={"status": "cleared"})
    assert r.status_code == 400 and "another reviewer" in r.json()["detail"]
    assert partner.patch(f"/api/review-notes/{note['id']}",
                         json={"status": "cleared"}).status_code == 200


def test_taking_back_needs_the_right_to_give():
    _, prep, rev, partner, eid, _ = team()
    for s in review.WORK:
        sign(prep, eid, s.id, "prepared")
        sign(rev, eid, s.id, "reviewed")
    sign(prep, eid, "completion", "prepared")
    sign(partner, eid, "completion", "reviewed")
    assert rev.delete(f"/api/engagements/{eid}/signoffs/completion/reviewed").status_code == 403
    assert partner.delete(f"/api/engagements/{eid}/signoffs/completion/reviewed").status_code \
        == 200


def test_only_the_preparer_answers_and_answers_can_be_sent_back():
    a, prep, rev, partner, eid, _ = team()
    sign(prep, eid, "data", "prepared")
    note = rev.post(f"/api/engagements/{eid}/review-notes",
                    json={"section": "data", "text": "Support?"}).json()
    r = partner.post(f"/api/review-notes/{note['id']}/replies", json={"text": "Agree, need it"})
    assert r.json()["status"] == "open"  # a fellow reviewer's comment isn't an answer
    r = prep.post(f"/api/review-notes/{note['id']}/replies", json={"text": "See invoice"})
    assert r.json()["status"] == "responded"
    r = rev.patch(f"/api/review-notes/{note['id']}", json={"status": "open"})
    assert r.json()["status"] == "open"
    log = prep.get("/api/activity", params={"engagement_id": eid}).json()[0]
    assert log["action"] == "note.reopen" and "Sent back" in log["summary"]


def test_raising_a_note_logs_the_withdrawn_review():
    _, prep, rev, partner, eid, _ = team()
    sign(prep, eid, "data", "prepared")
    sign(rev, eid, "data", "reviewed")
    partner.post(f"/api/engagements/{eid}/review-notes", json={"section": "data", "text": "?"})
    actions = [x["action"] for x in prep.get("/api/activity",
                                             params={"engagement_id": eid}).json()[:2]]
    assert actions == ["note.create", "signoff.remove"]
