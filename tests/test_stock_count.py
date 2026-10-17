"""Attending the inventory count: picking items from the listing, counting both ways, the
differences and what they suggest for the whole listing."""

import io

import pandas as pd
import pytest

from tests.test_fixed_assets import upload
from tests.test_related_summaries import steps
from tests.test_review import section, team

pytestmark = pytest.mark.usefixtures("real_auth")


def listing() -> str:
    lines = ["Item code,Description,Location,Qty,Unit cost,Value"]
    for i in range(1, 31):
        lines.append(f"A{i},Part {i},Main,{i * 10},5,{i * 50}")
    lines.append("A3,Part 3 again,Main,1,5,5")       # the same code twice
    lines.append("B1,Returned,Main,-4,5,-20")        # a negative quantity
    lines.append("B2,Free sample,Main,7,0,0")        # no cost
    return "\n".join(lines) + "\n"


def by_item(out):
    return {x["item"]: x for x in out["items"]}


def test_pick_count_both_ways_project_and_sign_off():
    a, prep, rev, partner, eid, _ = team()
    assert partner.put(f"/api/engagements/{eid}/materiality", json={
        "benchmark": "revenue", "benchmark_amount": 6_000_000}).status_code == 200
    lid = upload(prep, eid, "inventory", "stock.csv", listing())
    look = prep.post(f"/api/uploads/{lid}/inventory", json={"upload_id": lid})
    assert look.status_code == 200, look.text
    look = look.json()
    assert look["columns"] == {"item": "Item code", "description": "Description",
                               "location": "Location", "quantity": "Qty",
                               "unit_cost": "Unit cost", "value": "Value"}
    assert look["lines"] == 33 and look["value"] == 23_250 - 20 + 5
    found = {c["check"]: c["rows"] for c in look["checks"]["items"]}
    assert found == {"negative_quantity": [33], "no_cost": [34], "duplicate": [4, 32]}

    url = f"/api/engagements/{eid}/stock-count"
    assert prep.get(url).json()["count"] is None
    r = prep.post(url, json={"upload_id": lid, "largest": 3, "random": 5, "seed": 42})
    assert r.status_code == 201, r.text
    out = r.json()
    top = [x for x in out["items"] if x["reason"] == "largest"]
    assert [x["item"] for x in top] == ["A30", "A29", "A28"]
    rand = [x for x in out["items"] if x["reason"] == "random"]
    assert len(rand) == 5 and not {x["item"] for x in rand} & {"A30", "A29", "A28"}
    # The same seed picks the same items.
    again = prep.post(url, json={"upload_id": lid, "largest": 3, "random": 5, "seed": 42})
    assert [x["item"] for x in again.json()["items"]] == [x["item"] for x in out["items"]]
    assert out["summary"]["outcome"] == "counting"
    assert any("Record the count of 8 inventory items" in s for s in steps(prep, eid))

    items = by_item(again.json())
    for name, x in items.items():
        counted = x["book_qty"] - (2 if name == "A30" else 0)
        r = prep.patch(f"/api/stock-count-items/{x['id']}", json={"counted_qty": counted})
        assert r.status_code == 200, r.text
    first_random = rand[0]["item"]
    out = prep.patch(f"/api/stock-count-items/{items[first_random]['id']}",
                     json={"counted_qty": items[first_random]["book_qty"] - 1}).json()
    s = out["summary"]
    rand_value = sum(x["book_value"] for x in out["items"] if x["reason"] == "random")
    assert s["largest_error"] == -10 and s["random_error"] == -5
    rest = look["value"] - (300 + 290 + 280) * 5
    assert s["projected"] == round(-10 + (-5 / rand_value) * rest, 2)
    assert s["outcome"] == "within" and s["statuses"]["difference"] == 2
    assert any("Explain 2 differences found at the inventory count" in t
               for t in steps(prep, eid))
    # The book quantity of an item from the listing comes from the listing.
    r = prep.patch(f"/api/stock-count-items/{items['A30']['id']}", json={"book_qty": 1})
    assert r.status_code == 400

    # Floor to books: one in the listing, one not.
    r = prep.post(f"{url}/items", json={"item": "a2", "location": "Main", "counted_qty": 20})
    assert r.status_code == 201, r.text
    a2 = by_item(r.json())["a2"]
    assert (a2["book_qty"], a2["unit_cost"], a2["status"], a2["description"]) == \
        (20, 5, "agreed", "Part 2")
    out = prep.post(f"{url}/items", json={"item": "ZZ9", "description": "Pallet of boxes",
                                          "counted_qty": 3}).json()
    assert by_item(out)["ZZ9"]["status"] == "not_listed"

    r = rev.patch(url, json={"reviewed": True})
    assert r.status_code == 400 and "3 items" in r.json()["detail"]
    for name, why in (("A30", "Two damaged units written off after the count"),
                      (first_random, "Issued the day before, posted after"),
                      ("ZZ9", "Consignment stock held for a customer")):
        prep.patch(f"/api/stock-count-items/{by_item(out)[name]['id']}",
                   json={"explanation": why})
    assert prep.patch(url, json={"reviewed": True}).status_code == 403
    r = rev.patch(url, json={"reviewed": True})
    assert r.status_code == 400 and "date" in r.json()["detail"]
    prep.patch(url, json={"count_date": "2026-12-31", "location": "Main warehouse",
                          "conclusion": "The count was well run; differences explained."})
    out = rev.patch(url, json={"reviewed": True}).json()
    assert out["count"]["reviewed_by"] == "Reviewer"
    # A change afterwards takes the sign-off away.
    out = prep.patch(f"/api/stock-count-items/{a2['id']}", json={"note": "Tag 114"}).json()
    assert out["count"]["reviewed_at"] is None
    assert section(prep, eid, "stock")["needed"]
    # Items from the listing stay; floor items can go. Picking again needs a fresh start.
    assert prep.delete(f"/api/stock-count-items/{items['A30']['id']}").status_code == 400
    assert prep.delete(f"/api/stock-count-items/{a2['id']}").status_code == 200
    assert prep.post(url, json={"upload_id": lid}).status_code == 409

    book = pd.read_excel(io.BytesIO(prep.get(f"{url}/export").content), sheet_name=None)
    assert list(book) == ["About", "Count", "Listing checks"]
    assert len(book["Count"]) == 9
    assert prep.get(f"/api/engagements/{eid}/pack", params={"files": False}).status_code == 200

    # Deleting the listing keeps the count; floor items are no longer looked up.
    assert rev.delete(f"/api/uploads/{lid}").status_code == 204
    out = prep.post(f"{url}/items", json={"item": "A1", "counted_qty": 10}).json()
    assert out["count"]["upload_id"] is None and by_item(out)["A1"]["status"] == "not_listed"

    assert prep.delete(url).status_code == 403
    assert rev.delete(url).json()["count"] is None
    acts = {x["action"] for x in a.get("/api/activity").json()}
    assert {"stock.pick", "stock.count", "stock.add", "stock.update", "stock.unreview",
            "stock.remove", "stock.download", "stock.delete"} <= acts


def test_locked_files_and_bad_requests():
    a, prep, rev, partner, eid, gl = team()
    lid = upload(prep, eid, "inventory", "stock.csv", listing())
    url = f"/api/engagements/{eid}/stock-count"
    assert prep.patch(url, json={"location": "x"}).status_code == 404
    r = prep.post(url, json={"upload_id": gl})
    assert r.status_code == 400 and "inventory listing" in r.json()["detail"]
    r = prep.post(url, json={"upload_id": lid, "columns": {"item": "Item code"}})
    assert r.status_code == 400 and "quantity" in r.json()["detail"]
    r = prep.post(f"/api/uploads/{lid}/inventory",
                  json={"upload_id": lid, "columns": {"item": "Item code"}})
    assert "quantity" in r.json()["problem"] and "Qty" in r.json()["file_columns"]
    assert prep.post(url, json={"upload_id": lid, "largest": 0, "random": 0}).status_code == 400
    assert prep.post(url, json={"upload_id": lid, "random": 1000}).status_code == 422
    out = prep.post(url, json={"upload_id": lid, "largest": 40, "random": 5}).json()
    assert len(out["items"]) == 33  # everything with a quantity, once
    assert prep.post(f"{url}/items", json={}).status_code == 400

    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    item = out["items"][0]["id"]
    assert prep.patch(f"/api/stock-count-items/{item}",
                      json={"counted_qty": 1}).status_code == 409
    assert prep.post(f"{url}/items", json={"item": "A1"}).status_code == 409
    assert prep.post(f"/api/uploads/{lid}/inventory", json={"upload_id": lid}).status_code == 200
    assert prep.get(f"{url}/export").status_code == 200
