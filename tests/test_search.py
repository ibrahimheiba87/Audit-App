"""The search box: hits across the engagements the user may see, and nothing beyond them."""

import pytest

from tests.test_auth import add_user
from tests.test_review import team

pytestmark = pytest.mark.usefixtures("real_auth")


def kinds(c, q):
    r = c.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    return {g["kind"]: g["items"] for g in r.json()["groups"]}


def test_search_finds_work_in_the_users_engagements_only():
    a, prep, rev, partner, eid, _ = team()
    e = a.get(f"/api/engagements/{eid}").json()
    assert prep.post(f"/api/engagements/{eid}/findings",
                     json={"severity": "high", "detail": "Zebra payments without support"}
                     ).status_code == 201
    assert prep.post(f"/api/engagements/{eid}/tasks", json={"title": "Zebra cut-off test"}
                     ).status_code in (200, 201)
    assert prep.post(f"/api/engagements/{eid}/risks",
                     json={"area": "revenue", "title": "Zebra rebates", "level": "significant"}
                     ).status_code in (200, 201)

    hits = kinds(prep, "zebra")
    assert set(hits) == {"finding", "task", "risk"}
    f = hits["finding"][0]
    assert (f["engagement_id"], f["panel"], f["severity"]) == (eid, "reg-panel", "high")
    assert "Zebra payments" in f["title"]
    assert hits["task"][0]["panel"] == "wp-panel"

    # The engagement by its client's name; wildcards are taken as typed.
    found = kinds(prep, e["client"]["name"][:4])["engagement"]
    assert [x["engagement_id"] for x in found] == [eid] and found[0]["panel"] is None
    assert kinds(prep, "%%") == {}

    # Someone off the team finds nothing; an admin finds it all; clients can't search.
    other = add_user(a, "preparer", "other@example.com")
    assert kinds(other, "zebra") == {}
    assert set(kinds(a, "zebra")) == {"finding", "task", "risk"}
    assert prep.get("/api/search", params={"q": "z"}).status_code == 422


def test_many_hits_say_there_are_more():
    a, prep, rev, partner, eid, _ = team()
    for n in range(8):
        prep.post(f"/api/engagements/{eid}/tasks", json={"title": f"Yak count {n}"})
    out = prep.get("/api/search", params={"q": "yak"}).json()
    g = out["groups"][0]
    assert (g["kind"], len(g["items"]), g["more"]) == ("task", 6, True)
