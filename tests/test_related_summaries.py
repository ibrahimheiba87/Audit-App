"""The related parties register with its ledger scan, and AI summaries of contracts and
minutes (with the Claude API faked)."""

import io
import json

import pandas as pd
import pytest

from app import assistant
from tests.test_review import section, team

pytestmark = pytest.mark.usefixtures("real_auth")
PDF = ("lease.pdf", b"%PDF-1.4 fake lease", "application/pdf")
GL = """date,account,entry_id,description,amount
2026-03-01,6100 Rent,J1,Rent paid to ACME HOLDINGS LIMITED,5000
2026-12-29,1400 Loans,J2,Loan to director Sara Haddad,20000
2026-06-10,4000 Sales,J3,Sale to Acmeholdings Trading,-750
2026-07-01,6200 Fees,J4,Consulting fee S. Haddad,1234.5
2026-08-01,6300 Travel,J5,Taxi,40
"""


def upload_gl(c, eid):
    r = c.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
               files={"file": ("gl.csv", GL.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def steps(c, eid):
    return [s["text"] for s in c.get(f"/api/engagements/{eid}/dashboard").json()["next_steps"]]


def test_the_ledger_scan_finds_related_party_names_and_needs_a_conclusion():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/related-parties"
    r = prep.post(url, json={"name": "Acme Holdings Ltd", "relationship": "parent"})
    assert r.status_code == 201, r.text
    r = prep.post(url, json={"name": "Sara Haddad", "relationship": "director",
                             "aliases": ["S. Haddad"]})
    acme, sara = r.json()["parties"]
    assert prep.post(url, json={"name": "ACME HOLDINGS LIMITED"}).status_code == 400  # same
    gl = upload_gl(prep, eid)
    assert "Scan the general ledger for related party names." in steps(prep, eid)

    r = prep.post(f"{url}/scan", json={"upload_id": gl})
    assert r.status_code == 200, r.text
    found = {p["name"]: p for p in r.json()["scan"]["parties"]}
    # Whole names only: "Acmeholdings Trading" isn't Acme Holdings.
    assert [x["entry_id"] for x in found["Acme Holdings Ltd"]["rows"]] == ["J1"]
    loan = found["Sara Haddad"]["rows"][0]
    assert loan["entry_id"] == "J2" and loan["flags"] == ["Near the period end", "Round amount"]
    assert found["Sara Haddad"]["matches"] == 2 and found["Sara Haddad"]["debits"] == 21234.5
    assert "Review the transactions with 2 related parties." in steps(prep, eid)

    r = prep.patch(f"/api/related-parties/{sara['id']}", json={"reviewed": True})
    assert r.status_code == 400 and "conclusion" in r.json()["detail"]
    r = prep.patch(f"/api/related-parties/{sara['id']}", json={
        "reviewed": True, "conclusion": "Loan approved by the board.", "disclosed": "missing"})
    p = next(x for x in r.json()["parties"] if x["id"] == sara["id"])
    assert p["reviewed_by"] == "Preparer" and p["matches"] == 2
    assert "Disclose 1 related party the statements leave out." in steps(prep, eid)
    assert "Review the transactions with 1 related party." in steps(prep, eid)
    # A party with transactions found is a reviewer's to remove.
    assert prep.delete(f"/api/related-parties/{acme['id']}").status_code == 403
    # New names mean the last scan no longer applies.
    assert prep.patch(f"/api/related-parties/{sara['id']}",
                      json={"aliases": []}).status_code == 403
    r = rev.patch(f"/api/related-parties/{sara['id']}", json={"aliases": []})
    p = next(x for x in r.json()["parties"] if x["id"] == sara["id"])
    assert p["matches"] == 0 and p["reviewed_by"] == "" and p["scanned_at"] is None

    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert list(book) == ["About", "Related parties", "Transactions"]
    # Sara's names changed since the scan, so her old lines no longer stand.
    assert set(book["Transactions"]["Entry"]) == {"J1"}
    assert section(prep, eid, "related")["needed"]

    r = rev.post(f"/api/engagements/{eid}/roll-forward", json={"name": "FY2027"})
    carried = rev.get(f"/api/engagements/{r.json()['id']}/related-parties").json()["parties"]
    assert [(x["name"], x["relationship"], x["matches"], x["conclusion"]) for x in carried] == [
        ("Acme Holdings Ltd", "parent", 0, ""), ("Sara Haddad", "director", 0, "")]


@pytest.fixture
def fake_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    calls = []

    def fake(system, content, **kw):
        calls.append((system, content))
        return json.dumps({
            "title": "Office lease", "document_date": "2026-02-01",
            "parties": ["Acme Holdings Limited", "Nile Trading Co", "x" * 5000],
            "summary": "A five year lease of the head office.",
            "key_terms": [{"term": "Rent", "detail": "5,000 a month"}, "junk"],
            "amounts": [{"what": "Monthly rent", "amount": "5000", "currency": "USD"}],
            "dates": [{"date": "2027-01-15", "what": "Rent review"},
                      {"date": "not a date", "what": "x"}],
            "audit_points": [{"area": "related_parties", "point": "The landlord is the parent."},
                             {"area": "made_up", "point": "Check the deposit."}]})

    monkeypatch.setattr(assistant, "request_claude", fake)
    return calls


def test_a_contract_is_summarised_checked_and_linked_to_the_register(fake_api):
    a, prep, rev, partner, eid, _ = team()
    prep.post(f"/api/engagements/{eid}/related-parties", json={"name": "Acme Holdings"})
    url = f"/api/engagements/{eid}/document-summaries"
    r = prep.post(url, files={"file": PDF}, data={"kind": "contract", "language": "ar"})
    assert r.status_code == 201, r.text
    assert "Arabic" in fake_api[0][0] and fake_api[0][1][0]["type"] == "document"
    s = r.json()["summaries"][0]
    c = s["content"]
    assert c["title"] == "Office lease" and len(c["parties"][2]) <= 300
    assert c["amounts"][0]["amount"] == 5000 and len(c["key_terms"]) == 1
    assert [d["date"] for d in c["dates"]] == ["2027-01-15"]
    assert [p["area"] for p in c["audit_points"]] == ["related_parties", "other"]
    assert s["related"][0] == {"name": "Acme Holdings Limited", "party": "Acme Holdings"}
    assert s["related"][1]["party"] is None
    assert [d["what"] for d in s["after_period_end"]] == ["Rent review"]
    assert "Check 1 AI document summary against the documents." in steps(prep, eid)

    r = rev.patch(f"/api/document-summaries/{s['id']}", json={"reviewed": True, "note": "Agreed"})
    assert r.json()["summaries"][0]["reviewed_by"] == "Reviewer"
    assert r.json()["to_review"] == 0
    assert prep.post(url, files={"file": ("a.txt", b"hello", "text/plain")},
                     data={"kind": "minutes"}).status_code == 400
    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert book["Audit points"]["Point"].tolist() == ["The landlord is the parent.",
                                                      "Check the deposit."]
    other = partner.post(url, files={"file": PDF}, data={"kind": "contract"}).json()
    newest = other["summaries"][0]["id"]
    assert prep.delete(f"/api/document-summaries/{newest}").status_code == 403
    assert prep.delete(f"/api/document-summaries/{s['id']}").status_code == 200
    acts = [x["action"] for x in a.get("/api/activity").json()]
    assert {"summary.create", "summary.update", "summary.delete"} <= set(acts)


def test_summaries_are_refused_without_the_ai_or_on_a_locked_file(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/document-summaries"
    assert prep.post(url, files={"file": PDF}, data={"kind": "contract"}).status_code == 503
    assert prep.get(url).json()["enabled"] is False
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    r = prep.post(f"/api/engagements/{eid}/related-parties", json={"name": "Bob"})
    assert r.status_code == 409


def test_blank_cells_arabic_spellings_and_renames_after_review():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/related-parties"
    gl = ("date,account,entry_id,description,amount\n"
          "2026-05-01,,,دفعة إلى محمـد علي,300\n"
          "2026-05-02,6100,J9,إيجار شركة النور,400\n")
    r = prep.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
                  files={"file": ("ar.csv", gl.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    prep.post(url, json={"name": "مُحَمَّد علي", "relationship": "director"})
    prep.post(url, json={"name": "شركة النور ش.م.ع", "relationship": "group"})
    r = prep.post(f"{url}/scan", json={})
    assert r.status_code == 200, r.text
    assert [p["matches"] for p in r.json()["scan"]["parties"]] == [1, 1]
    assert prep.get(f"{url}/export").status_code == 200
    party = r.json()["parties"][0]["id"]
    r = prep.patch(f"/api/related-parties/{party}", json={"name": "Someone else"})
    assert r.status_code == 403
    assert rev.patch(f"/api/related-parties/{party}", json={"name": "Someone"}).status_code == 200


def test_a_short_name_in_a_document_isnt_taken_for_a_registered_party():
    from app.related_parties import find_party
    known = {"ali hassan": "Ali Hassan", "national bank": "National Bank Ltd"}
    assert find_party(known, "Ali") is None and find_party(known, "Bank") is None
    assert find_party(known, "Mr Ali Hassan") == "Ali Hassan"


def test_scans_stay_tied_to_their_ledger_and_lines():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/related-parties"
    prep.post(url, json={"name": "Sara Haddad", "relationship": "director"})
    # The name is only in Account Name; the loader maps Account Code and Narration.
    first = ("Date,Account Code,Account Name,Narration,Amount\n"
             "2026-03-02,1400,Loan to Sara Haddad,Advance,1000\n"
             "2026-03-03,6100,Rent,Paid,50\n")
    r = prep.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
                  files={"file": ("a.csv", first.encode(), "text/csv")})
    gl_a = r.json()["id"]
    party = prep.post(f"{url}/scan", json={"upload_id": gl_a}).json()["parties"][0]
    assert party["matches"] == 1
    r = prep.patch(f"/api/related-parties/{party['id']}",
                   json={"reviewed": True, "conclusion": "Approved loan."})
    assert r.json()["parties"][0]["reviewed_by"] == "Preparer"
    # Clearing the conclusion takes the review with it.
    r = prep.patch(f"/api/related-parties/{party['id']}", json={"conclusion": ""})
    assert r.json()["parties"][0]["reviewed_at"] is None
    prep.patch(f"/api/related-parties/{party['id']}",
               json={"reviewed": True, "conclusion": "Approved loan."})
    # The same ledger again: nothing new, so the review stands.
    r = prep.post(f"{url}/scan", json={"upload_id": gl_a})
    assert r.json()["parties"][0]["reviewed_by"] == "Preparer"

    # A newer ledger, scanned or not, isn't what the workpaper shows until it is scanned.
    other = ("date,account,description,amount\n2026-04-01,1400,Loan to Sara Haddad,9000\n")
    r = prep.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
                  files={"file": ("b.csv", other.encode(), "text/csv")})
    gl_b = r.json()["id"]
    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert book["Transactions"]["Amount"].tolist() == [1000]
    assert "a.csv" in book["About"]["Value"].astype(str).tolist()
    # As many lines, but different ones: a fresh review.
    r = prep.post(f"{url}/scan", json={"upload_id": gl_b})
    p = r.json()["parties"][0]
    assert p["matches"] == 1 and p["reviewed_at"] is None
    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert book["Transactions"]["Amount"].tolist() == [9000]

    # A party added since the last scan hasn't been looked for yet.
    assert "Scan the general ledger for related party names." not in steps(prep, eid)
    prep.post(url, json={"name": "Nile Trading"})
    assert "Scan the general ledger for related party names." in steps(prep, eid)

    # A carried register is work for this year's review too.
    r = rev.post(f"/api/engagements/{eid}/roll-forward", json={"name": "FY2027"})
    assert section(rev, r.json()["id"], "related")["needed"]


def test_a_document_dated_after_the_period_end_is_flagged(fake_api, monkeypatch):
    a, prep, rev, partner, eid, _ = team()
    answer = json.dumps({"title": "Board minutes", "document_date": "2027-02-01",
                         "summary": "The board approved a dividend.", "dates": []})
    monkeypatch.setattr(assistant, "request_claude", lambda *args, **kw: answer)
    url = f"/api/engagements/{eid}/document-summaries"
    r = prep.post(url, files={"file": PDF}, data={"kind": "minutes"})
    assert r.status_code == 201, r.text
    assert r.json()["summaries"][0]["after_period_end"] == [
        {"date": "2027-02-01", "what": "", "own": True}]
    from app import summaries
    from app.db import Engagement, SessionLocal
    with SessionLocal() as session:
        assert summaries.summary(session.get(Engagement, eid))["after_period_end"] == 1
