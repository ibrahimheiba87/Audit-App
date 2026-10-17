"""Tasks with owners and due dates, My tasks, and the time budget against hours worked."""

import io
from datetime import date, timedelta

import pandas as pd
import pytest

from tests.test_auth import add_user, join
from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")
TODAY = date.today()


def me(c):
    return c.get("/api/auth/me").json()["id"]


def steps(c, eid):
    return [s["text"] for s in c.get(f"/api/engagements/{eid}/dashboard").json()["next_steps"]]


def test_tasks_have_an_owner_a_due_date_and_show_under_my_tasks():
    a, prep, rev, partner, eid, _ = team()
    yesterday = (TODAY - timedelta(days=1)).isoformat()
    r = prep.post(f"/api/engagements/{eid}/tasks", json={
        "title": "Test revenue cut-off", "area": "sampling", "assignee_id": me(rev),
        "due_on": yesterday})
    assert r.status_code == 200, r.text
    t = r.json()["tasks"][0]
    assert t["assignee_name"] == "Reviewer" and t["overdue"] and t["area_label"] == "Sampling"
    assert r.json()["summary"]["overdue"] == 1
    assert "Catch up on 1 overdue task." in steps(prep, eid)

    mine = rev.get("/api/my-tasks").json()
    assert [x["title"] for x in mine["tasks"]] == ["Test revenue cut-off"]
    assert mine["overdue"] == 1 and mine["tasks"][0]["client_name"] == "Acme"
    assert prep.get("/api/my-tasks").json()["tasks"] == []

    # Only someone on the team can own a task.
    outsider = add_user(a, "preparer", "outsider@example.com")
    r = prep.patch(f"/api/tasks/{t['id']}", json={"assignee_id": me(outsider)})
    assert r.status_code == 400 and "team" in r.json()["detail"]

    r = rev.patch(f"/api/tasks/{t['id']}", json={"status": "done"})
    done = r.json()["tasks"][0]
    assert done["done_by"] == "Reviewer" and not done["overdue"]
    assert rev.get("/api/my-tasks").json()["tasks"] == []
    reopened = prep.patch(f"/api/tasks/{t['id']}", json={"status": "open",
                                                         "assignee_id": None}).json()["tasks"][0]
    assert reopened["done_by"] == "" and reopened["assignee_name"] == ""
    assert "Give 1 task an owner." in steps(prep, eid)


def test_my_tasks_leave_out_engagements_i_am_no_longer_on():
    a, prep, rev, partner, eid, _ = team()
    prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Bank letter",
                                                      "assignee_id": me(prep)})
    assert len(prep.get("/api/my-tasks").json()["tasks"]) == 1
    others = [u for u in (me(rev), me(partner))]
    assert a.put(f"/api/engagements/{eid}/team", json={"user_ids": others}).status_code == 200
    assert prep.get("/api/my-tasks").json()["tasks"] == []
    # Nobody is prompted by My tasks any more, so the dashboard asks for a new owner.
    assert "Give 1 task an owner." in steps(rev, eid)


def test_my_tasks_leave_out_locked_files():
    a, prep, rev, partner, eid, _ = team()
    prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Bank letter",
                                                      "assignee_id": me(prep)})
    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert prep.get("/api/my-tasks").json()["tasks"] == []


def test_only_whoever_added_a_task_or_a_reviewer_removes_it():
    a, prep, rev, partner, eid, _ = team()
    mine = prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Mine"}).json()["tasks"][0]
    theirs = rev.post(f"/api/engagements/{eid}/tasks",
                      json={"title": "Theirs"}).json()["tasks"][1]
    assert prep.delete(f"/api/tasks/{theirs['id']}").status_code == 403
    assert prep.delete(f"/api/tasks/{mine['id']}").status_code == 200
    r = rev.delete(f"/api/tasks/{theirs['id']}")
    assert r.status_code == 200 and r.json()["tasks"] == []


def test_hours_are_set_against_the_budget_by_area_and_person():
    a, prep, rev, partner, eid, _ = team()
    assert prep.put(f"/api/engagements/{eid}/budget",
                    json={"hours": {"data": 10}}).status_code == 403
    r = rev.put(f"/api/engagements/{eid}/budget", json={"hours": {"data": 10, "findings": 5}})
    assert r.status_code == 200 and r.json()["budget_total"] == 15
    day = TODAY.isoformat()
    for c, area, h in ((prep, "data", 7.5), (rev, "data", 4), (prep, "findings", 1)):
        r = c.post(f"/api/engagements/{eid}/time", json={"worked_on": day, "area": area,
                                                          "hours": h})
        assert r.status_code == 200, r.text
    w = r.json()
    rows = {x["area"]: x for x in w["budget"]}
    assert rows["data"]["actual"] == 11.5 and rows["data"]["over"]
    assert rows["data"]["remaining"] == -1.5 and rows["findings"]["used_pct"] == 20.0
    assert w["actual_total"] == 12.5 and w["my_hours"] == 8.5
    assert w["by_person"] == [{"name": "Preparer", "hours": 8.5},
                              {"name": "Reviewer", "hours": 4.0}]
    assert w["summary"]["over_areas"] == ["Files and checks"]
    assert "Hours on Files and checks are over budget." in steps(prep, eid)
    # Zero takes an area out of the budget; the hours worked stay.
    w = rev.put(f"/api/engagements/{eid}/budget", json={"hours": {"data": 0}}).json()
    rows = {x["area"]: x for x in w["budget"]}
    assert rows["data"]["budget"] is None and rows["data"]["actual"] == 11.5
    assert w["budget_total"] == 5
    assert rev.put(f"/api/engagements/{eid}/budget",
                   json={"hours": {"data": -1}}).status_code == 400


def test_hours_are_checked_and_only_their_owner_or_a_reviewer_removes_them():
    a, prep, rev, partner, eid, _ = team()
    url = f"/api/engagements/{eid}/time"
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    assert prep.post(url, json={"worked_on": tomorrow, "hours": 1}).status_code == 400
    assert prep.post(url, json={"worked_on": TODAY.isoformat(), "hours": 0}).status_code == 422
    # No more than 24 hours a day, across every engagement.
    client_id = a.get(f"/api/engagements/{eid}").json()["client"]["id"]
    other = a.post(f"/api/clients/{client_id}/engagements",
                   json={"name": "FY2025", "period_end": "2025-12-31"}).json()["id"]
    join(a, other, prep)
    assert prep.post(f"/api/engagements/{other}/time",
                     json={"worked_on": TODAY.isoformat(), "hours": 20}).status_code == 200
    r = prep.post(url, json={"worked_on": TODAY.isoformat(), "hours": 5})
    assert r.status_code == 400 and "24 hours" in r.json()["detail"]
    other_task = prep.post(f"/api/engagements/{other}/tasks",
                           json={"title": "x"}).json()["tasks"][0]
    r = prep.post(url, json={"worked_on": TODAY.isoformat(), "hours": 1,
                             "task_id": other_task["id"]})
    assert r.status_code == 400
    entry = rev.post(url, json={"worked_on": TODAY.isoformat(), "hours": 2}).json()["entries"][0]
    assert prep.delete(f"/api/time-entries/{entry['id']}").status_code == 403
    mine = prep.post(url, json={"worked_on": TODAY.isoformat(),
                                "hours": 1}).json()["entries"][0]
    assert prep.delete(f"/api/time-entries/{mine['id']}").status_code == 200
    assert rev.delete(f"/api/time-entries/{entry['id']}").json()["entries"] == []


def test_the_task_list_and_budget_roll_forward_without_owners_dates_or_hours():
    a, prep, rev, partner, eid, _ = team()
    prep.post(f"/api/engagements/{eid}/tasks", json={
        "title": "Bank letter", "area": "bank", "assignee_id": me(prep),
        "due_on": TODAY.isoformat()})
    rev.put(f"/api/engagements/{eid}/budget", json={"hours": {"bank": 6}})
    prep.post(f"/api/engagements/{eid}/time", json={"worked_on": TODAY.isoformat(),
                                                     "area": "bank", "hours": 3})
    r = rev.post(f"/api/engagements/{eid}/roll-forward", json={"name": "FY2027"})
    assert r.status_code == 201, r.text
    w = rev.get(f"/api/engagements/{r.json()['id']}/workplan").json()
    t = w["tasks"][0]
    assert (t["title"], t["area"], t["assignee_id"], t["due_on"], t["status"]) == \
        ("Bank letter", "bank", None, None, "open")
    assert w["budget_total"] == 6 and w["actual_total"] == 0 and w["entries"] == []
    off = rev.post(f"/api/engagements/{eid}/roll-forward",
                   json={"name": "FY2027b", "workplan": False}).json()["id"]
    assert rev.get(f"/api/engagements/{off}/workplan").json()["tasks"] == []


def test_the_work_plan_downloads_and_is_locked_with_the_file():
    a, prep, rev, partner, eid, _ = team()
    t = prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Stock count"}).json()
    prep.post(f"/api/engagements/{eid}/time", json={"worked_on": TODAY.isoformat(),
                                                     "hours": 2, "note": "Attended count"})
    r = prep.get(f"/api/engagements/{eid}/workplan/export")
    assert r.status_code == 200
    book = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert list(book) == ["About", "Tasks", "Budget vs actual", "Hours by person", "Time log",
                          "Staff bookings"]
    assert book["Time log"]["Note"].tolist() == ["Attended count"]
    acts = [x["action"] for x in a.get("/api/activity").json()]
    assert {"task.create", "time.log", "workplan.download"} <= set(acts)

    assert partner.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    for r in (prep.post(f"/api/engagements/{eid}/tasks", json={"title": "x"}),
              prep.patch(f"/api/tasks/{t['tasks'][0]['id']}", json={"status": "done"}),
              prep.post(f"/api/engagements/{eid}/time",
                        json={"worked_on": TODAY.isoformat(), "hours": 1})):
        assert r.status_code == 409, r.text
    assert prep.get(f"/api/engagements/{eid}/workplan").status_code == 200


def test_a_deactivated_owner_needs_replacing_and_hours_group_by_person():
    a, prep, rev, partner, eid, _ = team()
    prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Bank letter",
                                                      "assignee_id": me(rev)})
    assert "Give 1 task an owner." not in steps(prep, eid)
    assert a.patch(f"/api/users/{me(rev)}", json={"active": False}).status_code == 200
    assert "Give 1 task an owner." in steps(prep, eid)
    # Two people with the same name keep separate totals.
    twin = add_user(a, "preparer", "twin@example.com")
    join(a, eid, twin)
    for c, h in ((prep, 3), (twin, 2)):
        c.post(f"/api/engagements/{eid}/time", json={"worked_on": TODAY.isoformat(), "hours": h})
    people = prep.get(f"/api/engagements/{eid}/workplan").json()["by_person"]
    assert people == [{"name": "Preparer", "hours": 3.0}, {"name": "Preparer", "hours": 2.0}]
