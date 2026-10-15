"""The risk register and audit program, and aging receivables and payables listings."""

import io
from datetime import date

import pandas as pd
import pytest

from app import aging, db, loader
from tests.test_review import section, sign, team
from tests.test_statements import client, engagement, map_all

RISK = {"area": "revenue", "title": "Revenue recorded in the wrong period",
        "level": "significant", "assertions": ["cut_off", "occurrence"],
        "description": "Bonuses depend on sales.", "response": "Senior staff on revenue.",
        "procedures": ["Test sales either side of the year end", "  "]}


def actions(eid):
    with db.SessionLocal() as s:
        return [a.action for a in s.query(db.Activity).filter_by(engagement_id=eid)]


# --- Risk register and audit program -------------------------------------------


def test_add_risks_and_work_through_the_program():
    eid = engagement(with_prior=False)
    r = client.post(f"/api/engagements/{eid}/risks", json=RISK)
    assert r.status_code == 201, r.text
    risk = r.json()
    assert risk["area_label"] == "Revenue" and risk["level_label"] == "Significant risk"
    assert risk["assertion_labels"] == ["Cut-off", "Occurrence"]
    assert [p["text"] for p in risk["procedures"]] == ["Test sales either side of the year end"]
    low = client.post(f"/api/engagements/{eid}/risks",
                      json={"area": "fraud", "title": "Management override", "level": "lower"})
    assert low.status_code == 201
    reg = client.get(f"/api/engagements/{eid}/risks").json()
    assert [x["title"] for x in reg["risks"]] == [RISK["title"], "Management override"]
    assert reg["summary"] == {"risks": 2, "by_level": {"significant": 1, "higher": 0, "lower": 1},
                              "procedures": 1, "done": 0, "planned": 1,
                              "without_procedures": 1}
    steps = [s["text"] for s in client.get(f"/api/engagements/{eid}/dashboard").json()["next_steps"]]
    assert "Plan procedures for 1 risk with none yet." in steps
    assert "Finish 1 planned audit procedure." in steps

    # Moving the low risk up the list, and completing its new procedure.
    r = client.patch(f"/api/risks/{low.json()['id']}", json={"level": "higher", "title": " Override "})
    assert r.json()["level"] == "higher" and r.json()["title"] == "Override"
    proc = client.post(f"/api/risks/{low.json()['id']}/procedures",
                       json={"text": "Test journals"}).json()["procedures"][0]
    done = client.patch(f"/api/procedures/{proc['id']}", json={
        "status": "done", "reference": "C-4", "conclusion": "No exceptions."}).json()
    p = done["procedures"][0]
    assert p["status"] == "done" and p["done_by"] and p["done_at"] and p["reference"] == "C-4"
    reopened = client.patch(f"/api/procedures/{proc['id']}", json={"status": "planned"}).json()
    assert reopened["procedures"][0]["done_by"] == "" and reopened["procedures"][0]["done_at"] is None
    assert reopened["procedures"][0]["conclusion"] == "No exceptions."

    xl = client.get(f"/api/engagements/{eid}/risks/export")
    assert xl.status_code == 200
    sheets = pd.read_excel(io.BytesIO(xl.content), sheet_name=None)
    assert list(sheets) == ["About", "Risk register", "Audit program"]
    assert sheets["Risk register"]["Ref"].tolist() == ["R1", "R2"]
    assert sheets["Audit program"]["Ref"].tolist() == ["R1.1", "R2.1"]

    assert client.delete(f"/api/procedures/{proc['id']}").status_code == 204
    assert client.delete(f"/api/risks/{risk['id']}").status_code == 204
    with db.SessionLocal() as s:  # its procedures go with it
        assert s.query(db.RiskProcedure).count() == 0
    log = actions(eid)
    for a in ("risk.create", "risk.update", "procedure.create", "procedure.update",
              "procedure.delete", "risk.delete", "risk.download"):
        assert a in log, a


@pytest.mark.parametrize("bad", [{"area": "nowhere"}, {"assertions": ["truthiness"]},
                                 {"level": "medium"}, {"title": ""}])
def test_risks_are_checked(bad):
    eid = engagement(with_prior=False)
    r = client.post(f"/api/engagements/{eid}/risks", json={**RISK, **bad})
    assert r.status_code == 422


def test_import_the_ai_risk_assessment_once():
    eid = engagement(with_prior=False)
    items = [{"area": "revenue", "title": "Cut-off", "level": "significant",
              "assertions": ["cut_off"], "why": "Year-end rush.", "procedures": ["Test cut-off"]},
             {"area": "entity", "title": "Going concern", "level": "higher", "assertions": [],
              "why": "Losses.", "procedures": []}]
    with db.SessionLocal() as s:
        d = db.AiDraft(engagement_id=eid, kind="risk", content={"summary": "", "risks": items},
                       created_by="A")
        letter = db.AiDraft(engagement_id=eid, kind="letter", content={}, created_by="A")
        s.add_all([d, letter])
        s.commit()
        draft, letter_id = d.id, letter.id
    client.post(f"/api/engagements/{eid}/risks",
                json={"area": "revenue", "title": "cut-off ", "level": "lower"})
    r = client.post(f"/api/engagements/{eid}/risks/from-draft/{draft}").json()
    assert (r["added"], r["skipped"]) == (1, 1)
    gc = next(x for x in r["risks"] if x["title"] == "Going concern")
    assert gc["source"] == f"ai-draft:{draft}" and gc["description"] == "Losses."
    again = client.post(f"/api/engagements/{eid}/risks/from-draft/{draft}").json()
    assert (again["added"], again["skipped"]) == (0, 2)
    assert client.post(f"/api/engagements/{eid}/risks/from-draft/{letter_id}").status_code == 400
    assert "risk.import" in actions(eid)


@pytest.mark.usefixtures("real_auth")
def test_who_can_remove_and_review_staleness():
    a, prep, rev, _, eid, _ = team()
    risk = prep.post(f"/api/engagements/{eid}/risks", json=RISK).json()
    proc = risk["procedures"][0]["id"]
    assert prep.patch(f"/api/procedures/{proc}", json={"status": "done"}).status_code == 200
    assert prep.delete(f"/api/procedures/{proc}").status_code == 403
    assert prep.delete(f"/api/risks/{risk['id']}").status_code == 403
    # The section isn't needed until there's risk work; then a change makes its sign-off stale.
    assert section(prep, eid, "risks")["needed"]
    assert sign(prep, eid, "risks", "prepared").status_code == 201
    prep.patch(f"/api/risks/{risk['id']}", json={"response": "More testing."})
    assert section(prep, eid, "risks")["prepared"]["stale"]
    assert rev.delete(f"/api/procedures/{proc}").status_code == 204
    assert rev.delete(f"/api/risks/{risk['id']}").status_code == 204


# --- Aging --------------------------------------------------------------------

AR = b"""Customer Code,Customer Name,Invoice No,Invoice Date,Due Date,Amount Due
C1,Alpha,INV1,2026-10-01,2026-10-31,500000
C1,Alpha,INV2,2026-12-01,2026-12-31,200000
C2,Beta,INV3,2026-03-01,2026-04-01,250000
C2,Beta,CN1,2026-11-15,,-2000
C3,Gamma,INV4,2027-01-05,2027-02-04,700
"""


def listing(rows, columns=("account", "date", "amount")):
    df = pd.DataFrame(rows, columns=list(columns))
    df.insert(0, "row", range(2, len(df) + 2))
    if "date" in df:
        df["date"] = pd.to_datetime(df["date"])
    if "due" in df:
        df["due"] = pd.to_datetime(df["due"])
    return df


def test_buckets_cover_every_day():
    assert [aging.bucket_of(d) for d in (-5, 0, 1, 30, 31, 90, 91, 180, 181, None)] == [
        "current", "current", "d1_30", "d1_30", "d31_60", "d61_90", "d91_180", "d91_180",
        "over_180", "no_date"]


def test_payables_without_due_dates_age_from_the_terms():
    df = listing([("V1", "2026-12-15", -1000.0), ("V1", "2026-09-01", -400.0),
                  ("V2", "2026-11-01", 50.0), ("V3", None, -10.0)])
    r = aging.age(df, "ap", date(2026, 12, 31), terms_days=30)
    assert r["flipped"] and r["basis"] == "terms" and r["totals"]["balance"] == 1360.0
    b = {x["id"]: x["balance"] for x in r["buckets"]}
    assert (b["current"], b["d91_180"], b["no_date"]) == (1000.0, 400.0, 10.0)
    assert b["d1_30"] == -50.0  # a debit on a payables listing: due 2026-12-01, 30 days over
    assert r["totals"]["credit_items"] == 1 and r["totals"]["long_overdue"] == 400.0
    v1 = next(p for p in r["parties"] if p["party"] == "V1")
    assert v1["balance"] == 1400.0 and v1["oldest_days"] == 91 and v1["long_overdue"] == 400.0
    with pytest.raises(aging.AgingError):
        aging.age(df, "ap", date(2026, 12, 31), terms_days=400)


def test_loader_reads_a_receivables_listing():
    raw = loader.read_file("ar.csv", AR)
    assert loader.suggest_mapping(list(raw.columns)) == {
        "due": "Due Date", "amount": "Amount Due", "date": "Invoice Date",
        "account": "Customer Code", "description": "Customer Name", "entry_id": "Invoice No"}
    bad = AR.replace(b"2026-10-31", b"soon")
    report = loader.normalize(loader.read_file("ar.csv", bad)).report.to_dict()
    assert any("due date that couldn't be read" in w for w in report["warnings"])


def test_age_a_listing_against_the_trial_balance():
    eid = engagement(with_prior=False)  # receivables 948,200.00 in the trial balance
    up = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "ar"},
                     files={"file": ("ar.csv", AR, "text/csv")})
    assert up.status_code == 201, up.text
    uid = up.json()["id"]
    r = client.post(f"/api/uploads/{uid}/aging", json={}).json()
    assert r["ledger"] is None  # nothing mapped yet
    map_all(eid)
    r = client.post(f"/api/uploads/{uid}/aging", json={"terms_days": 45}).json()
    assert r["as_of"] == "2026-12-31" and r["basis"] == "due" and "lines" not in r
    assert r["totals"]["balance"] == 948_700.0 and r["totals"]["parties"] == 3
    assert r["ledger"]["balance"] == 948_200.0 and r["ledger"]["difference"] == 500.0
    assert r["totals"]["after_items"] == 1 and r["totals"]["long_overdue"] == 250_000.0
    cn = next(x for x in r["exceptions"] if x["invoice"] == "CN1")
    assert cn["why"] == ["Credit balance"] and cn["due"] == "2026-12-30"  # date + 45 days
    xl = client.get(f"/api/uploads/{uid}/aging/export", params={"terms_days": 45})
    assert xl.status_code == 200
    sheets = pd.read_excel(io.BytesIO(xl.content), sheet_name=None)
    assert list(sheets) == ["About", "Aging", "By customer", "Exceptions", "All items"]
    assert len(sheets["All items"]) == 5
    assert client.get(f"/api/uploads/{uid}/aging/export",
                      params={"terms_days": 999}).status_code == 400
    gl = next(u for u in client.get(f"/api/engagements/{eid}").json()["uploads"]
              if u["kind"] == "tb")
    assert client.post(f"/api/uploads/{gl['id']}/aging", json={}).status_code == 400
    log = actions(eid)
    assert "analysis.aging" in log and "analysis.download" in log


def test_aging_export_escapes_formulas():
    df = listing([("=HYPERLINK(1)", "2026-12-01", 10.0)])
    r = aging.with_ledger(aging.age(df, "ar", date(2026, 12, 31)), None)
    book = pd.read_excel(io.BytesIO(aging.to_excel(r, {"Client": "=cmd"})), sheet_name=None)
    assert "=cmd" in book["About"]["Value"].tolist()
    assert book["By customer"]["Customer"].tolist() == ["=HYPERLINK(1)"]
