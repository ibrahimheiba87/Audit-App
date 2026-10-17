"""The fixed asset register: totals, additions and disposals, depreciation recomputed, odd
entries and the tie-out to the trial balance."""

import io

import pandas as pd
import pytest

from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")
REGISTER = ("Asset No,Description,Category,Acquisition date,Disposal date,Cost,"
            "Accumulated depreciation,Depreciation charge,Useful life\n") + """\
FA1,Lathe,Plant,2020-01-01,,100000,60000,10000,10
FA2,Van,Vehicles,2026-07-01,,40000,4000,4000,5
FA3,Laptop,IT,2023-01-01,,3000,3000,1000,3
FA4,Old car,Vehicles,2019-01-01,2026-03-31,20000,20000,0,5
FA5,Desk,Furniture,2021-01-01,,5000,2000,1500,10
FA6,Server,IT,2024-01-01,,6000,7000,2000,3
FA5,Chair,Furniture,,,800,0,0,
"""
TB = """account,description,amount
1000,Cash,21200
1500,Equipment,78800
3000,Share capital,-100000
4000,Sales,-50000
6800,Depreciation,18500
8000,Other expenses,31500
"""
MAPPING = {"1000": "cash", "1500": "ppe", "3000": "share_capital", "4000": "revenue",
           "6800": "depreciation", "8000": "other_expenses"}


def upload(c, eid, kind, name, text):
    r = c.post(f"/api/engagements/{eid}/uploads", data={"kind": kind},
               files={"file": (name, text.encode(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_the_register_is_summed_recomputed_and_tied_to_the_trial_balance():
    a, prep, rev, partner, eid, _ = team()
    fid = upload(prep, eid, "fixed_assets", "register.csv", REGISTER)
    url = f"/api/uploads/{fid}/fixed-assets"
    r = prep.post(url, json={})
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["columns"] == {"asset": "Asset No", "description": "Description",
                            "category": "Category", "acquired": "Acquisition date",
                            "disposed": "Disposal date", "cost": "Cost",
                            "accumulated": "Accumulated depreciation",
                            "charge": "Depreciation charge", "life": "Useful life"}
    assert x["totals"] == {"assets": 6, "cost": 154800, "accumulated": 76000, "nbv": 78800,
                           "charge": 18500}
    assert [c["category"] for c in x["categories"]] == ["Furniture", "IT", "Plant", "Vehicles"]
    assert x["additions"]["count"] == 1 and x["additions"]["items"][0]["asset"] == "FA2"
    assert x["disposals"]["count"] == 1 and x["disposals"]["items"][0]["asset"] == "FA4"
    # The van's half year is within 1%; the desk is charged three times over and the server
    # past its cost.
    rec = x["recomputed"]
    assert rec["checked"] == 6
    assert [(i["asset"], i["expected"], i["difference"]) for i in rec["items"]] == \
        [("FA5", 500, 1000), ("FA6", 1000, 1000)]
    issues = {(i["asset"], i["description"]): i["issues"] for i in x["issues"]["items"]}
    assert "Accumulated depreciation is more than cost" in issues[("FA6", "Server")]
    assert "No date acquired" in issues[("FA5", "Chair")]
    assert "The same asset number more than once" in issues[("FA5", "Desk")]
    # No trial balance yet, so nothing to tie to.
    assert x["tie"] == []

    upload(prep, eid, "tb", "tb.csv", TB)
    assert prep.put(f"/api/engagements/{eid}/mapping",
                    json={"mappings": MAPPING}).status_code == 200
    x = prep.post(url, json={}).json()
    assert [(t["trial_balance"], t["difference"]) for t in x["tie"]] == [(78800, 0), (18500, 0)]

    # Without a charge column, nothing is recomputed and only the NBV is tied.
    cols = {k: v for k, v in x["columns"].items() if k != "charge"}
    x = prep.post(url, json={"columns": cols}).json()
    assert x["recomputed"]["checked"] == 0 and [t["line"] for t in x["tie"]] == ["ppe"]

    book = pd.read_excel(io.BytesIO(prep.post(f"{url}/export", json={}).content),
                         sheet_name=None)
    assert list(book) == ["About", "By category", "Tie to the TB", "Additions", "Disposals",
                          "Depreciation differences", "Points to look at"]
    assert book["Depreciation differences"]["Recomputed"].tolist() == [500, 1000]
    acts = [x["action"] for x in a.get("/api/activity").json()]
    assert "analysis.fixed_assets" in acts and "analysis.download" in acts


def test_columns_must_exist_cost_is_needed_and_other_files_are_refused():
    a, prep, rev, partner, eid, gl = team()
    fid = upload(prep, eid, "fixed_assets", "register.csv", REGISTER)
    url = f"/api/uploads/{fid}/fixed-assets"
    r = prep.post(url, json={"columns": {"cost": "Nope"}})
    assert r.status_code == 400 and "no column called Nope" in r.json()["detail"]
    # Until the cost column is picked, the page gets the columns to pick from.
    r = prep.post(url, json={"columns": {"asset": "Asset No"}})
    assert r.status_code == 200 and "cost" in r.json()["problem"]
    assert "Cost" in r.json()["file_columns"] and "totals" not in r.json()
    r = prep.post(f"{url}/export", json={"columns": {"asset": "Asset No"}})
    assert r.status_code == 400
    r = prep.post(f"/api/uploads/{gl}/fixed-assets", json={})
    assert r.status_code == 400 and "fixed asset register" in r.json()["detail"]
    # It's kept as a list: the checks don't run on it.
    r = prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": fid})
    assert r.status_code == 400 and "Fixed asset register" in r.json()["detail"]
    # It only reads, so it still works once the file is locked.
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert prep.post(url, json={}).status_code == 200
    assert prep.post(f"{url}/export", json={}).status_code == 200
