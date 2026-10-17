"""The ledger explorer: filters, the pivot and the Excel download."""

import io

import pandas as pd
import pytest

from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")
GL = """date,account,entry_id,description,user,amount
2026-01-05,4000 Sales,J1,Invoice 1 to Acme,ann,-1000
2026-01-12,1100 Bank,J1,Invoice 1 to Acme,ann,1000
2026-02-07,6100 Rent,J2,February rent,bob,500
2026-02-07,1100 Bank,J2,February rent,bob,-500
2026-03-16,4000 Sales,J3,Manual adjustment,,-25000
2026-03-16,1100 Bank,J3,Manual adjustment,,25000
"""


def upload(c, eid):
    r = c.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
               files={"file": ("gl.csv", GL.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_filters_and_a_pivot_by_account_and_month():
    a, prep, rev, partner, eid, _ = team()
    uid = upload(prep, eid)
    url = f"/api/uploads/{uid}/explore"
    r = prep.post(url, json={"account": "sales", "columns": "month"})
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["matched"] == 2 and x["credits"] == 26000 and x["net"] == -26000
    p = x["pivot"]
    assert p["columns"] == ["2026-01", "2026-03"]
    assert p["rows"] == [{"key": "4000 Sales", "lines": 2, "debits": 0, "credits": 26000,
                          "net": -26000, "cells": [-1000, -25000]}]
    # Weekend postings (7 February 2026 is a Saturday), largest first, by size.
    x = prep.post(url, json={"weekend": True}).json()
    assert {line["entry_id"] for line in x["lines"]} == {"J2"}
    x = prep.post(url, json={"min_amount": 1000, "side": "debit", "sort": "largest"}).json()
    assert [line["amount"] for line in x["lines"]] == [25000, 1000]
    x = prep.post(url, json={"text": "rent", "rows": "user"}).json()
    assert [r["key"] for r in x["pivot"]["rows"]] == ["bob"]
    x = prep.post(url, json={"date_from": "2026-03-01", "rows": "user"}).json()
    assert [r["key"] for r in x["pivot"]["rows"]] == ["(blank)"]
    r = prep.post(url, json={"date_from": "2026-04-01", "date_to": "2026-01-01"})
    assert r.status_code == 400 and "start date" in r.json()["detail"]

    r = prep.get(f"{url}/export", params={"account": "bank", "columns": "user"})
    assert r.status_code == 200
    book = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert list(book) == ["About", "Pivot", "Lines"]
    assert len(book["Lines"]) == 3 and list(book["Pivot"].columns)[-1] == "Net"
    acts = [x["action"] for x in a.get("/api/activity").json()]
    assert {"analysis.explore", "analysis.download"} <= set(acts)


def test_the_explorer_still_works_on_a_locked_file():
    a, prep, rev, partner, eid, _ = team()
    uid = upload(prep, eid)
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert prep.post(f"/api/uploads/{uid}/explore", json={}).json()["matched"] == 6


def test_a_wide_pivot_keeps_its_totals_and_the_download_has_more_columns():
    from app import explorer

    a, prep, rev, partner, eid, _ = team()
    lines = "".join(f"2026-01-{1 + i % 28:02d},{4000 + i} Sales,J{i},x,u{i},{-(i + 1)}\n"
                    for i in range(50))
    r = prep.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
                  files={"file": ("wide.csv", (GL.splitlines()[0] + "\n" + lines).encode(),
                                  "text/csv")})
    uid = r.json()["id"]
    url = f"/api/uploads/{uid}/explore"
    p = prep.post(url, json={"rows": "month", "columns": "user"}).json()["pivot"]
    assert p["columns_total"] == 50
    assert len(p["columns"]) == explorer.MAX_PIVOT_COLUMNS + 1 and p["columns"][-1] == "(other)"
    row = p["rows"][0]
    assert round(sum(row["cells"]), 2) == row["net"] == -sum(range(1, 51))
    r = prep.get(f"{url}/export", params={"rows": "month", "columns": "user"})
    pv = pd.read_excel(io.BytesIO(r.content), sheet_name="Pivot")
    assert len(pv.columns) == 4 + 50 + 1 and "(other)" not in pv.columns
