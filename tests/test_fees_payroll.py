"""Fees, billing and the firm's WIP, and the payroll tests."""

import io
import re

import pandas as pd
import pytest

from app import fees, payroll
from tests.test_statements import client, engagement, map_all

PAYROLL = (
    "Employee No,Employee Name,Bank Account,Department,Pay Date,Gross Pay,Net Pay,Date Left\n"
    "E1,Ali Hassan,EG01 1111,Sales,2026-01-31,10000,8000,\n"
    "E1,Ali Hassan,EG01 1111,Sales,2026-02-28,10000,8000,\n"
    "E1,Ali Hassan,EG01 1111,Sales,2026-03-31,30000,24000,\n"
    "E2,Mona Adel,EG02-2222,Admin,2026-01-31,8000,8000,\n"
    "E2,Mona Adel,EG022222,Admin,2026-01-31,8000,6500,\n"
    "E3,Ghost,EG011111,Sales,2026-02-28,5000,4000,\n"
    "E4,Sara Nabil,EG04 4444,Admin,2026-03-31,7000,7500,2026-01-15\n"
    "E5,Sara Nabil,EG05 5555,Admin,2026-03-31,7000,6000,\n"
)


def payroll_upload(eid):
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "payroll"},
                    files={"file": ("payroll.csv", PAYROLL, "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_payroll_tests_find_the_odd_lines():
    eid = engagement(with_prior=False)
    map_all(eid)
    up = payroll_upload(eid)
    out = client.post(f"/api/uploads/{up}/payroll", json={})
    assert out.status_code == 200, out.text
    r = out.json()
    assert r["columns"]["bank"] == "Bank Account" and r["columns"]["left"] == "Date Left"
    assert r["people"] == 5 and r["rows"] == 8 and r["gross"] == 85000
    by = {}
    for x in r["issues"]["items"]:
        by.setdefault(x["row"], set()).update(x["issues"])
    # E1 and E3 share an account, once spaces are ignored.
    assert "The same bank account as another employee" in by[2] and \
        "The same bank account as another employee" in by[7]
    assert "Paid more than once in the month" in by[5] and "Nothing deducted from gross pay" in by[5]
    assert {"Paid after leaving", "Net pay is more than gross pay",
            "The same name with different employee numbers"} <= by[8]
    assert "At least twice this person's usual pay" in by[4]
    assert by[3] == {"The same bank account as another employee"}  # otherwise an ordinary month
    assert [m["month"] for m in r["months"]] == ["2026-01", "2026-02", "2026-03"]
    assert r["tie"]["register"] == 85000 and r["tie"]["trial_balance"] is not None

    book = pd.read_excel(io.BytesIO(client.post(f"/api/uploads/{up}/payroll/export",
                                                json={"columns": r["columns"]}).content),
                         sheet_name=None)
    assert {"About", "By month", "By department", "Points to look at"} <= set(book)
    bad = client.post(f"/api/uploads/{up}/payroll",
                      json={"columns": {"name": "Employee Name"}}).json()
    assert bad["problem"].startswith("Say which column holds the gross")
    acts = {a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": eid}).json()}
    assert "analysis.payroll" in acts
    # A payroll register is a list: there are no amounts for the audit checks.
    assert client.post(f"/api/engagements/{eid}/runs", json={"upload_id": up}).status_code == 400


def test_the_payroll_tests_skip_what_the_register_cannot_support():
    eid = engagement(with_prior=False)
    map_all(eid)
    e = client.get(f"/api/engagements/{eid}").json()
    year = int(e["period_end"][:4])
    csv = ("Employee No,Name,Account,Month,Gross Pay\n"
           "E1,Ali,,January,1000\nE1,Ali,,February,1000\nE2,Mona,,Jan,900\n"
           ",,,,2900\n")
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "payroll"},
                    files={"file": ("p.csv", csv, "text/csv")})
    out = client.post(f"/api/uploads/{r.json()['id']}/payroll",
                      json={"columns": {"employee": "Employee No", "name": "Name",
                                        "bank": "Account", "paid_on": "Month",
                                        "gross": "Gross Pay"}}).json()
    # The total line is left out, months are read in the year audited, and the empty bank
    # column flags nothing.
    assert out["rows"] == 3 and out["skipped_rows"] == 1 and out["gross"] == 2900
    assert [m["month"] for m in out["months"]][0].startswith(str(year)) or \
        [m["month"] for m in out["months"]][0].startswith(str(year - 1))
    assert out["issue_counts"] == []
    assert len(out["notes"]) == 2 and out["tie"]["register"] == 2900

    csv = "Employee No,Pay Date,Gross Pay\nE1,not a date,100\nE2,,200\n"
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "payroll"},
                    files={"file": ("q.csv", csv, "text/csv")})
    out = client.post(f"/api/uploads/{r.json()['id']}/payroll", json={}).json()
    assert out["months"] == [] and out["tie"]["register"] == 300
    assert payroll.NOTES["dates"] in out["notes"]


def test_months_are_read_in_the_year_audited():
    from datetime import date  # noqa: PLC0415

    start, end = date(2025, 4, 1), date(2026, 3, 31)
    values = pd.Series(["March", "Apr-25", "1", "مايو", "2025-06-30", None])
    parsed = pd.Series([pd.NaT] * 4 + [pd.Timestamp("2025-06-30"), pd.NaT])
    got = payroll.pay_dates(values, parsed, start, end)
    assert [None if pd.isna(d) else d.date().isoformat() for d in got] == [
        "2026-03-31", "2025-04-30", None, "2025-05-31", "2025-06-30", None]
    numbers = payroll.pay_dates(pd.Series([1, 12, 4]), pd.Series([pd.NaT] * 3), start, end)
    assert [d.date().isoformat() for d in numbers] == ["2026-01-31", "2025-12-31", "2025-04-30"]


def test_fees_billing_and_wip():
    eid = engagement(with_prior=False)
    me = client.get("/api/auth/me").json()
    assert client.put("/api/fees/rates", json={"rates": {"nope": 1}}).status_code == 400
    rates = client.put("/api/fees/rates", json={"rates": {me["role"]: 100}}).json()["rates"]
    assert any(r["role"] == me["role"] and r["rate"] == 100 for r in rates)
    client.put(f"/api/engagements/{eid}/planning", json={"fee": 5000, "currency": "EGP"})
    for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
        r = client.post(f"/api/engagements/{eid}/time", json={"worked_on": day, "hours": 20})
        assert r.status_code == 200, r.text
    out = client.get(f"/api/engagements/{eid}/fees").json()
    assert out["hours"] == 60 and out["time_value"] == 6000 and out["fee"] == 5000
    assert out["recovery_pct"] == 83.3 and out["to_bill"] == 5000 and out["flags"] == []

    r = client.post(f"/api/engagements/{eid}/invoices", json={
        "number": "INV-1", "issued_on": "2026-01-10", "amount": 3000})
    assert r.status_code == 201, r.text
    x = r.json()["invoices"][0]
    assert x["overdue"] and r.json()["outstanding"] == 3000 and r.json()["to_bill"] == 2000
    assert "overdue" in r.json()["flags"]
    assert client.post(f"/api/engagements/{eid}/invoices", json={
        "issued_on": "2026-02-10", "amount": 1, "paid_on": "2026-01-01"}).status_code == 400
    r = client.put(f"/api/invoices/{x['id']}", json={
        "number": "INV-1", "issued_on": "2026-01-10", "amount": 3000, "paid_on": "2026-02-01"})
    assert r.json()["outstanding"] == 0 and r.json()["paid"] == 3000

    # Invoices stay out of the engagement's own log, which the whole team reads.
    acts = client.get("/api/activity", params={"engagement_id": eid}).json()
    assert not any(a["action"].startswith("invoice.") for a in acts)
    assert any(a["action"] == "invoice.create" and a["details"]["engagement_id"] == eid
               for a in client.get("/api/activity", params={"action": "invoice."}).json())
    # A blank rate clears it.
    rates = client.put("/api/fees/rates", json={"rates": {me["role"]: None}}).json()["rates"]
    assert next(r for r in rates if r["role"] == me["role"])["rate"] is None
    client.put("/api/fees/rates", json={"rates": {me["role"]: 100}})

    wip = client.get("/api/wip").json()
    row = next(w for w in wip["rows"] if w["engagement_id"] == eid)
    assert row["billed"] == 3000 and "invoices" not in row
    book = pd.read_excel(io.BytesIO(client.get("/api/wip/export").content), sheet_name=None)
    assert {"About", "Engagements"} <= set(book)
    assert client.delete(f"/api/invoices/{x['id']}").json()["invoices"] == []

    # Invoices can still be recorded once the file is locked.
    assert client.post(f"/api/engagements/{eid}/lock", json={}).status_code in (200, 201)
    r = client.post(f"/api/engagements/{eid}/invoices", json={
        "issued_on": "2026-03-01", "amount": 2000})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("module", [fees, payroll])
def test_new_text_has_arabic(module):
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    source = open(module.__file__, encoding="utf-8").read()
    texts = re.findall(r'(?:HTTPException\(\d+, |Error\()f?"([^"]+)"\)', source)
    texts = [re.sub(r"\{[^}]+\}", "9", t) for t in texts]
    texts += list(getattr(module, "FIELDS", {}).values())
    texts += re.findall(r'issues?\.extend|flag\([^"]+"([^"]+)"\)', source)
    missing = sorted({t for t in texts if t and not translated(t)})
    assert not missing, missing
