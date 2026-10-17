"""Firm checklists added to engagements, and firm request lists sent to clients."""

import io
import re
import uuid
import zipfile

import pandas as pd
import pytest

from app import checklists, request_lists
from tests.test_statements import client, engagement


def firm_checklist(items=("Is the contract signed?", "Is revenue recognised over time?")):
    name = f"Construction {uuid.uuid4().hex[:6]}"
    r = client.post("/api/checklists", json={"name": name, "items": list(items)})
    assert r.status_code == 201, r.text
    return r.json()


def test_a_firm_checklist_is_added_answered_and_completed():
    c = firm_checklist()
    assert client.post("/api/checklists", json={"name": c["name"], "items": ["x"]}).status_code == 409
    eid = engagement(with_prior=False)
    out = client.get(f"/api/engagements/{eid}/checklists").json()
    assert any(x["id"] == c["id"] for x in out["library"]) and out["checklists"] == []

    out = client.post(f"/api/engagements/{eid}/checklists", json={"checklist_id": c["id"]})
    assert out.status_code == 201, out.text
    [x] = out.json()["checklists"]
    assert x["total"] == 2 and x["answered"] == 0 and not x["changed_at_firm"]
    assert client.post(f"/api/engagements/{eid}/checklists",
                       json={"checklist_id": c["id"]}).status_code == 409

    url = f"/api/engagement-checklists/{x['id']}"
    assert client.post(f"{url}/complete").status_code == 400  # nothing answered yet
    r = client.put(url, json={"answers": [{"answer": "yes"}, {"answer": "no"}]})
    assert r.json()["checklists"][0]["needs_comment"] == 1
    assert client.post(f"{url}/complete").status_code == 400  # a No without a comment
    client.put(url, json={"answers": [{"answer": "yes"}, {"answer": "no", "comment": "Point in time"}],
                          "conclusion": "Fine"})
    done = client.post(f"{url}/complete").json()["checklists"][0]
    assert done["completed_at"] and done["no"] == 1
    assert client.put(url, json={"answers": [{"answer": "yes"}]}).status_code == 400

    # Any change reopens it.
    r = client.put(url, json={"answers": [{"answer": "na"}, {"answer": "no", "comment": "Point in time"}],
                              "conclusion": "Fine"})
    assert r.json()["checklists"][0]["completed_at"] is None

    # The firm changes its checklist: the engagement keeps its copy until it takes the new one.
    client.put(f"/api/checklists/{c['id']}", json={
        "name": c["name"], "items": ["Is revenue recognised over time?", "Any claims?"]})
    x = client.get(f"/api/engagements/{eid}/checklists").json()["checklists"][0]
    assert x["changed_at_firm"] and x["total"] == 2
    old = [i["text"] for i in x["items"]]
    x = client.post(f"{url}/latest").json()["checklists"][0]
    assert [i["text"] for i in x["items"]] == ["Is revenue recognised over time?", "Any claims?"]
    assert x["items"][0]["answer"] == "no" and x["items"][1]["answer"] is None
    # Answers to the questions as they were before are refused.
    assert client.put(url, json={"answers": [{"answer": "yes"}, {"answer": "yes"}],
                                 "items": old}).status_code == 400

    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/checklists/export").content), sheet_name=None)
    assert len(book["Answers"]) == 2
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/api/engagements/{eid}/pack").content))
    assert "firm-checklists" in " ".join(z.namelist())

    new = client.post(f"/api/engagements/{eid}/roll-forward", json={}).json()
    assert new["carried"]["checklists"] == 1
    [y] = client.get(f"/api/engagements/{new['id']}/checklists").json()["checklists"]
    assert y["answered"] == 0 and y["total"] == 2

    # Deleting the firm's checklist leaves the engagements' copies.
    assert client.delete(f"/api/checklists/{c['id']}").status_code == 204
    x = client.get(f"/api/engagements/{eid}/checklists").json()["checklists"][0]
    assert not x["changed_at_firm"] and x["source_id"] is None
    assert client.post(f"{url}/latest").status_code == 400
    assert client.delete(url).json()["checklists"] == []
    acts = {a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": eid}).json()}
    assert {"checklist.add", "checklist.update", "checklist.complete", "checklist.latest",
            "checklist.remove"} <= acts
    sections = {s["id"]: s for s in client.get(
        f"/api/engagements/{new['id']}/review").json()["sections"]}
    assert sections["checklists"]["needed"]


def test_a_request_list_is_sent_and_saved():
    name = f"Year end {uuid.uuid4().hex[:6]}"
    items = [{"title": "Bank statements", "due_days": 15},
             {"title": "Fixed asset register", "details": "With additions", "due_days": 30},
             {"title": "Board minutes"}]
    r = client.post("/api/request-templates", json={"name": name, "items": items})
    assert r.status_code == 201, r.text
    t = r.json()
    assert client.post("/api/request-templates", json={
        "name": f"{name} 2", "items": [{"title": "A"}, {"title": "a"}]}).status_code == 400

    eid = engagement(with_prior=False)
    client.post(f"/api/engagements/{eid}/requests", json={"title": "board minutes"})
    out = client.post(f"/api/engagements/{eid}/requests/from-template",
                      json={"template_id": t["id"]}).json()
    assert out == {"added": 2, "skipped": 1}
    reqs = {r["title"]: r for r in client.get(f"/api/engagements/{eid}/requests").json()["requests"]}
    assert reqs["Bank statements"]["due_date"] == "2027-01-15"
    assert reqs["Fixed asset register"]["details"] == "With additions"
    assert sorted(r["number"] for r in reqs.values()) == [1, 2, 3]
    again = client.post(f"/api/engagements/{eid}/requests/from-template",
                        json={"template_id": t["id"]}).json()
    assert again == {"added": 0, "skipped": 3}
    # Sending it again took no numbers.
    assert client.post(f"/api/engagements/{eid}/requests", json={"title": "Payroll"}).json()["number"] == 4

    saved = client.post(f"/api/engagements/{eid}/requests/save-template",
                        json={"name": f"{name} copy"})
    assert saved.status_code == 201, saved.text
    by = {i["title"]: i for i in saved.json()["items"]}
    assert by["Bank statements"]["due_days"] == 15 and by["board minutes"]["due_days"] is None
    assert client.post(f"/api/engagements/{eid}/requests/save-template",
                       json={"name": f"{name} copy"}).status_code == 409

    r = client.put(f"/api/request-templates/{t['id']}", json={"name": name, "items": items[:1]})
    assert len(r.json()["items"]) == 1
    assert client.delete(f"/api/request-templates/{t['id']}").status_code == 204
    assert client.post(f"/api/engagements/{eid}/requests/from-template",
                       json={"template_id": t["id"]}).status_code == 404


@pytest.mark.parametrize("module", [checklists, request_lists])
def test_new_text_has_arabic(module):
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    source = open(module.__file__, encoding="utf-8").read()
    texts = re.findall(r'HTTPException\(\d+, f?"([^"]+)"', source)
    texts = [re.sub(r"\{[^}]+\}", "9", t) for t in texts]
    texts += list(getattr(module, "ANSWERS", {}).values())
    missing = sorted({t for t in texts if not translated(t)})
    assert not missing, missing
