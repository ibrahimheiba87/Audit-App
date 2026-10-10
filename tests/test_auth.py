from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.main import app
from tests.conftest import ADMIN

GL = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"
pytestmark = pytest.mark.usefixtures("real_auth")


def login(email, password):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return c


def admin():
    return login(ADMIN["email"], ADMIN["password"])


def add_user(c, role, email=None):
    email = email or f"{role}@example.com"
    r = c.post("/api/users", json={"email": email, "name": role.title(), "role": role,
                                   "password": "long enough pw"})
    assert r.status_code == 201, r.text
    return login(email, "long enough pw")


def test_password_hashing():
    h = auth.hash_password("s3cret password")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert auth.verify_password("s3cret password", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


def test_api_needs_sign_in_but_page_and_health_do_not():
    c = TestClient(app)
    assert c.get("/api/clients").status_code == 401
    assert c.post("/api/audit", files={"file": (GL.name, GL.read_bytes())}).status_code == 401
    assert c.get("/").status_code == 200
    assert c.get("/api/health").status_code == 200
    status = c.get("/api/auth/status").json()
    assert status["user"] is None and status["needs_setup"] is False


def test_first_run_setup():
    with db.SessionLocal() as s:
        s.query(db.User).delete()
        s.commit()
    c = TestClient(app)
    assert c.get("/api/auth/status").json()["needs_setup"] is True
    weak = c.post("/api/auth/setup", json={"email": "a@b.co", "name": "A", "password": "short"})
    assert weak.status_code == 400
    r = c.post("/api/auth/setup", json={"email": "Boss@Firm.com", "name": "Boss",
                                        "password": "a long password"})
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "admin" and r.json()["user"]["email"] == "boss@firm.com"
    assert c.get("/api/clients").status_code == 200  # signed in by setup
    again = TestClient(app).post("/api/auth/setup", json={"email": "x@y.z", "name": "X",
                                                          "password": "a long password"})
    assert again.status_code == 409


def test_login_logout_and_cookie():
    c = TestClient(app)
    bad = c.post("/api/auth/login", json={"email": ADMIN["email"], "password": "nope"})
    assert bad.status_code == 401
    r = c.post("/api/auth/login", json={"email": ADMIN["email"].upper(),
                                        "password": ADMIN["password"]})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert c.get("/api/auth/me").json()["name"] == "Test Admin"
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/auth/me").status_code == 401


def test_repeated_failed_logins_lock_out():
    c = TestClient(app)
    for _ in range(auth.MAX_FAILED_LOGINS):
        c.post("/api/auth/login", json={"email": ADMIN["email"], "password": "wrong"})
    r = c.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    assert r.status_code == 429


def test_change_password():
    c = admin()
    url = "/api/auth/password"
    assert c.post(url, json={"current_password": "x", "new_password": "new long pw!"}).status_code == 400
    r = c.post(url, json={"current_password": ADMIN["password"], "new_password": "new long pw!"})
    assert r.status_code == 204
    login(ADMIN["email"], "new long pw!")


def test_user_management_is_admin_only():
    a = admin()
    prep = add_user(a, "preparer")
    assert prep.get("/api/users").status_code == 403
    assert a.post("/api/users", json={"email": "preparer@example.com", "name": "Dup",
                                      "role": "preparer", "password": "long enough pw"}
                  ).status_code == 409
    users = {u["email"]: u for u in a.get("/api/users").json()}
    uid = users["preparer@example.com"]["id"]
    r = a.patch(f"/api/users/{uid}", json={"role": "reviewer"})
    assert r.json()["role"] == "reviewer"
    a.patch(f"/api/users/{uid}", json={"active": False})
    assert prep.get("/api/clients").status_code == 401  # deactivated: signed out
    me = a.get("/api/auth/me").json()
    last_admin = a.patch(f"/api/users/{me['id']}", json={"role": "partner"})
    assert last_admin.status_code == 400


def setup_engagement(c):
    cl = c.post("/api/clients", json={"name": "Acme"}).json()
    e = c.post(f"/api/clients/{cl['id']}/engagements",
               json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    up = c.post(f"/api/engagements/{e['id']}/uploads",
                files={"file": (GL.name, GL.read_bytes(), "text/csv")}).json()
    return e["id"], up["id"]


def test_role_permissions():
    a = admin()
    prep, rev, partner = (add_user(a, r) for r in ("preparer", "reviewer", "partner"))
    assert prep.post("/api/clients", json={"name": "Nope"}).status_code == 403
    eid, uid = setup_engagement(rev)  # reviewers can add clients and engagements
    mat = {"benchmark": "revenue", "benchmark_amount": 6_000_000}
    denied = rev.put(f"/api/engagements/{eid}/materiality", json=mat)
    assert denied.status_code == 403 and "partner or admin" in denied.json()["detail"]
    assert partner.put(f"/api/engagements/{eid}/materiality", json=mat).status_code == 200
    # Preparers do the work...
    assert prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid}).status_code == 201
    fid = prep.get(f"/api/engagements/{eid}/findings").json()["findings"][0]["id"]
    assert prep.patch(f"/api/findings/{fid}", json={"status": "reviewed"}).status_code == 200
    # ...but clearing, severity changes and deletions need a reviewer.
    clear = {"status": "cleared", "note": "Agreed to invoice"}
    assert prep.patch(f"/api/findings/{fid}", json=clear).status_code == 403
    assert prep.patch(f"/api/findings/{fid}", json={"severity": "low"}).status_code == 403
    r = rev.patch(f"/api/findings/{fid}", json=clear)
    assert r.status_code == 200 and r.json()["trail"][-1]["author"] == "Reviewer"
    assert prep.patch(f"/api/findings/{fid}", json={"status": "open"}).status_code == 403
    bulk = {"ids": [fid], "status": "open"}
    assert prep.post(f"/api/engagements/{eid}/findings/bulk", json=bulk).status_code == 403
    assert prep.delete(f"/api/engagements/{eid}").status_code == 403
    assert rev.delete(f"/api/engagements/{eid}").status_code == 403
    assert partner.delete(f"/api/engagements/{eid}").status_code == 204


def test_activity_log():
    a = admin()
    prep = add_user(a, "preparer")
    eid, uid = setup_engagement(a)
    prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})
    prep.get("/api/runs/1/report")
    log = prep.get("/api/activity", params={"engagement_id": eid}).json()
    actions = [x["action"] for x in log]
    assert actions[:3] == ["run.download", "run.create", "upload.create"]
    run = log[1]
    assert run["user_name"] == "Preparer" and run["user_role"] == "preparer"
    assert len(run["details"]["sha256"]) == 64 and run["details"]["settings"]["cutoff_days"] == 5
    assert log[2]["details"]["filename"] == GL.name
    assert prep.get("/api/activity").status_code == 403  # the full log is for admins
    every = a.get("/api/activity").json()
    assert {"auth.login", "user.create", "client.create"} <= {x["action"] for x in every}
    failed = TestClient(app).post("/api/auth/login", json={"email": "who@x.y", "password": "z"})
    assert failed.status_code == 401
    assert a.get("/api/activity", params={"action": "auth.login_failed"}).json()[0]["summary"] \
        == "Failed sign-in for who@x.y"


def test_activity_survives_engagement_deletion():
    a = admin()
    eid, _ = setup_engagement(a)
    a.delete(f"/api/engagements/{eid}")
    log = a.get("/api/activity", params={"engagement_id": eid}).json()
    assert log[0]["action"] == "engagement.delete"
    assert log[0]["engagement_label"] == "Acme · FY2026"
