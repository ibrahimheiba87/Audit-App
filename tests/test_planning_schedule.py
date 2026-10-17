"""The engagement letter and planning memo, and staff scheduling across engagements."""

import io
import zipfile

import pandas as pd
import pytest

from tests.test_auth import join
from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")


def me(c):
    return c.get("/api/auth/me").json()["id"]


def word_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return z.read("word/document.xml").decode()


def test_letter_and_memo_from_the_file():
    a, prep, rev, partner, eid, _ = team()
    p = prep.get(f"/api/engagements/{eid}/planning").json()
    assert p["fee"] is None and p["approach"] == "substantive"
    assert any("acceptance decision" in w for w in p["warnings"])
    assert prep.put(f"/api/engagements/{eid}/planning",
                    json={"currency": "U1"}).status_code == 400
    r = prep.put(f"/api/engagements/{eid}/planning", json={
        "addressee": "the board of Acme", "fee": 25000, "currency": "usd",
        "fee_basis": "fixed", "report_due": "2027-03-31", "letter_date": "2026-11-01",
        "accepted_on": "2026-10-01", "accepted_by": "CFO", "business": "Makes widgets.",
        "approach": "combined", "other_services": "Tax return preparation."})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["currency"] == "USD" and p["updated_by"] == "Preparer"
    assert "The client's acceptance is dated before the letter." in p["warnings"]
    prep.post(f"/api/engagements/{eid}/risks", json={
        "area": "revenue", "title": "Revenue cut-off", "level": "significant",
        "response": "Test sales either side of year end."})

    letter = prep.get(f"/api/engagements/{eid}/planning/letter?language=en")
    assert letter.status_code == 200
    text = word_text(letter.content)
    assert "the board of Acme" in text and "USD 25,000.00" in text
    assert "31 March 2027" in text and "Tax return preparation." in text
    ar = word_text(prep.get(f"/api/engagements/{eid}/planning/letter?language=ar").content)
    assert "خطاب ارتباط المراجعة" in ar and "<w:bidi/>" in ar

    memo = word_text(prep.get(f"/api/engagements/{eid}/planning/memo").content)
    assert "Makes widgets." in memo and "Revenue cut-off" in memo
    assert "combined approach" in memo and "Preparer, preparer" in memo
    assert "Materiality is not set yet." in memo

    acts = [x["action"] for x in prep.get("/api/activity",
                                          params={"engagement_id": eid}).json()]
    assert {"plan.update", "plan.download_letter", "plan.download_memo"} <= set(acts)
    # Saving the same values changes nothing and logs nothing.
    n = acts.count("plan.update")
    prep.put(f"/api/engagements/{eid}/planning", json={"fee": 25000})
    acts = [x["action"] for x in prep.get("/api/activity",
                                          params={"engagement_id": eid}).json()]
    assert acts.count("plan.update") == n
    # Clearing the fee asks for it again.
    p = prep.put(f"/api/engagements/{eid}/planning", json={"fee": None}).json()
    assert p["fee"] is None and "Set the fee for the engagement letter." in p["warnings"]

    pack = a.get(f"/api/engagements/{eid}/pack")
    assert pack.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(pack.content)).namelist()
    assert any("engagement-letter" in n for n in names)
    assert any("planning-memo" in n for n in names)


def test_bookings_on_the_team_and_the_firm_schedule():
    a, prep, rev, partner, eid, _ = team()
    pid = me(prep)
    body = {"user_id": pid, "starts_on": "2027-01-04", "ends_on": "2027-01-10", "hours": 35}
    assert prep.post(f"/api/engagements/{eid}/bookings", json=body).status_code == 403
    r = rev.post(f"/api/engagements/{eid}/bookings", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["booked_hours"] == 35 and r.json()["warnings"] == []
    assert rev.post(f"/api/engagements/{eid}/bookings",
                    json={**body, "ends_on": "2027-01-01"}).status_code == 400
    assert rev.post(f"/api/engagements/{eid}/bookings",
                    json={**body, "hours": 200}).status_code == 400  # over 24 a day
    # Admins see every engagement but aren't on its team, so they can't be booked on it.
    assert rev.post(f"/api/engagements/{eid}/bookings",
                    json={**body, "user_id": me(a), "hours": 8}).status_code == 400

    # A second engagement the preparer is on but the reviewer isn't.
    cl = a.post("/api/clients", json={"name": "Hidden Co"}).json()
    other = a.post(f"/api/clients/{cl['id']}/engagements",
                   json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    join(a, other, prep)
    r = a.post(f"/api/engagements/{other}/bookings",
               json={**body, "starts_on": "2027-01-06", "ends_on": "2027-01-06", "hours": 8,
                     "note": "Stock count"})
    assert r.json()["warnings"] == [
        "Preparer is now booked for 43 hours in the week of 2027-01-04, more than a full "
        "week of 40."]

    s = rev.get("/api/schedule", params={"start": "2027-01-06", "weeks": 2}).json()
    assert s["start"] == "2027-01-04" and s["weeks"] == ["2027-01-04", "2027-01-11"]
    row = next(p for p in s["people"] if p["id"] == pid)
    assert row["weeks"] == [43, 0] and row["over"] == [True, False] and s["over"] == 1
    # The booking on the other engagement counts but isn't listed.
    assert [b["engagement_id"] for b in row["bookings"]] == [eid] and row["other_hours"] == 8
    # The preparer is on both, so sees both.
    s = prep.get("/api/schedule", params={"start": "2027-01-04", "weeks": 1}).json()
    row = next(p for p in s["people"] if p["id"] == pid)
    assert len(row["bookings"]) == 2 and row["other_hours"] == 0
    assert prep.get("/api/schedule", params={"weeks": 27}).status_code == 422

    book = pd.read_excel(io.BytesIO(rev.get("/api/schedule/export",
                                            params={"start": "2027-01-04"}).content),
                         sheet_name=None)
    assert "Hidden Co" not in book["Bookings"]["Engagement"].astype(str).str.cat()
    assert book["Hours by week"]["Total"].max() == 43

    bid = next(b["id"] for b in rev.get(f"/api/engagements/{eid}/bookings").json()["bookings"]
               if b["user_id"] == pid)
    assert prep.delete(f"/api/staff-bookings/{bid}").status_code == 403
    r = rev.delete(f"/api/staff-bookings/{bid}")
    assert r.status_code == 200 and all(b["id"] != bid for b in r.json()["bookings"])
    acts = {x["action"] for x in rev.get("/api/activity", params={"engagement_id": eid}).json()}
    assert {"booking.create", "booking.delete"} <= acts


def test_only_the_team_can_be_booked():
    a, prep, rev, partner, eid, _ = team()
    cl = a.post("/api/clients", json={"name": "Other"}).json()
    other = a.post(f"/api/clients/{cl['id']}/engagements",
                   json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    r = a.post(f"/api/engagements/{other}/bookings", json={
        "user_id": me(prep), "starts_on": "2027-01-04", "ends_on": "2027-01-08", "hours": 8})
    assert r.status_code == 400 and "team" in r.json()["detail"]
