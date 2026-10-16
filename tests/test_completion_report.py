"""The completion checklist, subsequent events, the representation letter and the draft
auditor's report."""

import io
import zipfile

import pytest

from app import completion
from tests.test_review import section, team

pytestmark = pytest.mark.usefixtures("real_auth")


def all_answers(**overrides):
    out = {q: {"answer": expected or "yes"} for q, (_, _, expected) in completion.QUESTION.items()}
    for q, a in overrides.items():
        out[q] = {"answer": a}
    return out


def word_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return z.read("word/document.xml").decode()


def test_the_completion_checklist_is_signed_off_once_nothing_is_open():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/completion"
    s = prep.get(url).json()
    assert s["answered"] == 0 and "Conclude on going concern." in s["blockers"]
    r = prep.put(url, json={"going_concern": "uncertainty_disclosed"})
    assert r.status_code == 400 and "going concern" in r.json()["detail"]
    r = prep.put(url, json={"reps_dated": "2026-12-31"})
    assert r.status_code == 400 and "period end" in r.json()["detail"]

    r = prep.post(f"{url}/events", json={"description": "Fire at the warehouse",
                                         "occurred_on": "2027-01-20", "kind": "non_adjusting",
                                         "treatment": "adjusted"})
    assert r.status_code == 400 and "non-adjusting" in r.json()["detail"]
    r = prep.post(f"{url}/events", json={"description": "Fire at the warehouse",
                                         "occurred_on": "2027-01-20", "amount": 50000})
    ev = r.json()["events"][0]
    assert r.json()["pending_events"] == 1

    prep.put(url, json={"answers": all_answers(gc_doubt="yes"), "going_concern": "appropriate",
                        "reps_requested_on": "2027-02-01", "reps_dated": "2027-02-10"})
    assert prep.post(f"{url}/signoff", json={}).status_code == 403
    r = partner.post(f"{url}/signoff", json={})
    assert r.status_code == 400 and "subsequent event" in r.json()["detail"]
    prep.patch(f"/api/subsequent-events/{ev['id']}", json={"treatment": "disclosed"})
    r = partner.post(f"{url}/signoff", json={})
    assert r.status_code == 400 and "second look" in r.json()["detail"]
    r = partner.post(f"{url}/signoff", json={"note": "Forecasts show headroom."})
    assert r.status_code == 200 and r.json()["signed_by"] == "Partner"
    assert not r.json()["changed_since_signoff"]
    assert section(prep, eid, "finalisation")["needed"]

    # A later change shows the sign-off may need a look.
    prep.patch(f"/api/subsequent-events/{ev['id']}", json={"note": "Insured"})
    assert prep.get(url).json()["changed_since_signoff"]
    book = prep.get(f"{url}/export")
    assert book.status_code == 200


def test_the_representation_letter_lists_events_in_either_language():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/completion"
    prep.post(f"{url}/events", json={"description": "Loan refinanced", "kind": "non_adjusting",
                                     "treatment": "disclosed", "occurred_on": "2027-01-05"})
    prep.put(url, json={"reps_dated": "2027-02-10"})
    r = prep.get(f"{url}/representation-letter")
    assert r.status_code == 200 and r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml")
    text = word_text(r.content)
    assert "Loan refinanced" in text and "10 February 2027" in text and "Acme" in text
    ar = word_text(prep.get(f"{url}/representation-letter", params={"language": "ar"}).content)
    assert "<w:bidi/>" in ar and "خطاب الإفادات" in ar and "10 فبراير 2027" in ar


def test_the_report_draft_follows_the_opinion_and_warns_when_the_file_disagrees():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/report"
    s = prep.get(url).json()
    assert s["opinion"] == "unmodified" and s["suggestion"]["opinion"] == "unmodified"
    prep.put(f"/api/engagements/{eid}/completion", json={
        "going_concern": "inappropriate", "going_concern_note": "The bank withdrew its loan"})
    s = prep.get(url).json()
    assert s["suggestion"]["opinion"] == "adverse"
    assert any("adverse" in w for w in s["warnings"])

    r = prep.put(url, json={"opinion": "adverse", "firm_name": "Heiba & Co",
                            "report_date": "2027-02-01", "emphasis": ["Note 3 on the fire."],
                            "key_matters": [{"title": "Revenue", "why": "Large", "how": "Tests"}]})
    s = r.json()
    assert r.status_code == 200 and "Describe the basis for the modified opinion." in s["warnings"]
    paras = prep.get(f"{url}/preview").json()["paragraphs"]
    texts = [p["text"] for p in paras]
    assert "Adverse opinion" in texts and "Heiba & Co" in texts and "1 February 2027" in texts
    assert any(t.startswith("[Describe the matter") for t in texts)
    assert "Key audit matters" in texts and "Emphasis of matter" in texts

    prep.put(url, json={"opinion": "disclaimer", "basis": "No access to the records."})
    texts = [p["text"] for p in prep.get(f"{url}/preview").json()["paragraphs"]]
    assert "Disclaimer of opinion" in texts and "Key audit matters" not in texts
    r = prep.get(f"{url}/download", params={"language": "ar"})
    assert r.status_code == 200 and "الامتناع عن إبداء الرأي" in word_text(r.content)
    assert section(prep, eid, "finalisation")["needed"]
    pack = prep.get(f"/api/engagements/{eid}/pack")
    with zipfile.ZipFile(io.BytesIO(pack.content)) as z:
        names = z.namelist()
    assert any(n.endswith(".docx") for n in names) and any("completion" in n for n in names)


def test_saving_parts_keeps_the_rest_and_a_later_change_reopens_the_sign_off():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/completion"
    # Saving an empty report draft on a new file changes nothing and doesn't fail.
    assert prep.put(f"/api/engagements/{eid}/report", json={}).status_code == 200
    assert prep.put(f"/api/engagements/{eid}/report",
                    json={"report_date": None}).json()["updated_by"] == ""
    prep.put(url, json={"answers": all_answers(gc_doubt="yes"), "going_concern": "appropriate",
                        "conclusion": "All done.", "reps_dated": "2027-02-10"})
    r = prep.put(url, json={"answers": {"se_minutes": {"comment": "Read to February"}}})
    assert r.json()["answers"]["se_minutes"] == {"answer": "yes", "comment": "Read to February"}
    # The preparer's conclusion doesn't stand in for the partner's note.
    r = partner.post(f"{url}/signoff", json={})
    assert r.status_code == 400 and "second look" in r.json()["detail"]
    assert partner.post(f"{url}/signoff", json={"note": "Headroom."}).status_code == 200
    report = f"/api/engagements/{eid}/report"
    prep.put(report, json={"report_date": "2027-02-12"})
    assert "The completion procedures aren't signed off yet." not in prep.get(report).json()[
        "warnings"]
    prep.put(url, json={"going_concern": "uncertainty_disclosed",
                        "going_concern_note": "Loan due in March"})
    assert "The completion procedures aren't signed off yet." in prep.get(report).json()[
        "warnings"]


def test_partial_key_audit_matters_are_kept_and_long_names_refused():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/report"
    r = prep.put(url, json={"key_matters": [{"title": "", "why": "Large balance"}]})
    assert r.json()["key_matters"][0]["why"] == "Large balance"
    texts = [p["text"] for p in prep.get(f"{url}/preview").json()["paragraphs"]]
    assert "[Name the key audit matter]" in texts
    assert prep.put(url, json={"city": "x" * 201}).status_code == 422
