"""The partner overview of every engagement, and who posted what in the general ledger."""

import io
from datetime import date, timedelta

import pandas as pd
import pytest

from app import db
from tests.test_fixed_assets import upload
from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")


def test_the_overview_shows_each_file_and_what_holds_it_up():
    a, prep, rev, partner, eid, _ = team()
    client = a.get(f"/api/engagements/{eid}").json()["client"]["id"]
    other = a.post(f"/api/clients/{client}/engagements",
                   json={"name": "FY2027", "period_end": "2027-12-31"}).json()["id"]
    assert prep.get("/api/overview").status_code == 403  # reviewers and up
    assert prep.post(f"/api/engagements/{eid}/findings",
                     json={"severity": "high", "detail": "Unsupported journal"}).status_code == 201
    me = prep.get("/api/auth/me").json()
    with db.SessionLocal() as s:
        s.add(db.Task(engagement_id=eid, title="Cut-off", assignee_id=me["id"],
                      due_on=date.today() - timedelta(days=3)))
        s.commit()

    # The partner only sees the engagement they are on; an admin sees both.
    out = partner.get("/api/overview").json()
    assert [r["id"] for r in out["rows"]] == [eid] and not out["all"]
    r = out["rows"][0]
    assert (r["stage"], r["findings_high"], r["tasks_overdue"]) == ("fieldwork", 1, 1)
    assert r["partners"] == ["Partner"]
    assert r["waiting"] == ["1 high-severity finding open", "1 task overdue"]
    assert out["totals"]["needs_attention"] == 1
    rows = a.get("/api/overview").json()["rows"]
    assert [x["id"] for x in rows] == [eid, other]  # most attention first
    assert rows[1]["stage"] == "planning" and rows[1]["attention"] == 0

    # Locked files need nothing; archived ones are left out unless asked for.
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    r = partner.get("/api/overview").json()["rows"][0]
    assert r["stage"] == "locked" and r["attention"] == 0
    assert a.post(f"/api/engagements/{eid}/archive", json={"archived": True}).status_code == 200
    assert partner.get("/api/overview").json()["rows"] == []
    assert len(partner.get("/api/overview", params={"archived": True}).json()["rows"]) == 1

    book = pd.read_excel(io.BytesIO(a.get("/api/overview/export").content), sheet_name=None)
    assert list(book) == ["About", "Engagements"] and len(book["Engagements"]) == 1
    assert "overview.download" in {x["action"] for x in a.get("/api/activity").json()}


LEDGER = ["Entry,Date,Account,Description,Debit,Credit,Posted by,Approved by"]
for n in range(1, 41):
    day = date(2026, 1, 1) + timedelta(days=n)
    LEDGER += [f"J{n},{day},6000 Rent,Rent {n},100,,Ann,Bob", f"J{n},{day},1000 Bank,Rent {n},,100,Ann,Bob"]
LEDGER += [
    "J41,2026-03-01,1000 Bank,Cash sale,500,,Bob,Bob",           # approved by himself
    "J41,2026-03-01,4000 Sales,Cash sale,,500,Bob,Bob",
    "J42,2026-03-07,6000 Rent,Weekend top-up,900,,Zed,",          # occasional, no approver
    "J42,2026-03-07,1000 Bank,Weekend top-up,,900,Zed,",
    "J43,2026-03-08,6000 Rent,Unknown,50,,,Bob",                  # no user
    "J43,2026-03-08,1000 Bank,Unknown,,50,,Bob",
]


def test_who_posted_what():
    a, prep, rev, partner, eid, _ = team()
    gl = upload(prep, eid, "gl", "ledger.csv", "\n".join(LEDGER) + "\n")
    r = prep.post(f"/api/uploads/{gl}/posters")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["entries"], out["users"], out["has_approver"]) == (43, 4, True)
    people = {u["user"]: u for u in out["by_user"]}
    assert people["Ann"]["entries"] == 40 and people["Ann"]["debits"] == 4000
    assert people["Bob"]["self_approved"] == 1 and people["Bob"]["cash_revenue"] == 1
    assert people["Zed"]["weekend"] == 1 and "(blank)" in people
    checks = {c["check"]: [i["entry_id"] for i in c["items"]] for c in out["checks"]}
    assert checks == {"self_approved": ["J41"], "no_approver": ["J42"], "no_user": ["J43"],
                      "cash_revenue": ["J41"], "occasional": ["J42", "J41"]}

    book = pd.read_excel(io.BytesIO(prep.get(f"/api/uploads/{gl}/posters/export").content),
                         sheet_name=None)
    assert list(book) == ["About", "By user", "Own approval", "No approver", "No user",
                          "Cash and revenue", "Occasional posters"]
    acts = {x["action"] for x in a.get("/api/activity").json()}
    assert {"analysis.posters", "analysis.download"} <= acts
    # It only reads, so it still works once the file is locked.
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert prep.post(f"/api/uploads/{gl}/posters").status_code == 200


def test_a_ledger_needs_a_user_column():
    a, prep, rev, partner, eid, gl = team()
    nouser = upload(prep, eid, "gl", "plain.csv",
                    "Entry,Date,Account,Debit,Credit\nJ1,2026-01-02,1000,5,\nJ1,2026-01-02,4000,,5\n")
    r = prep.post(f"/api/uploads/{nouser}/posters")
    assert r.status_code == 400 and "who posted" in r.json()["detail"]
    tb = upload(prep, eid, "tb", "tb.csv", "account,amount\n1000,5\n4000,-5\n")
    assert prep.post(f"/api/uploads/{tb}/posters").status_code == 400
