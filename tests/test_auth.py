import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, db
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


def test_setup_needs_the_setup_code_when_one_is_set(monkeypatch):
    monkeypatch.setenv("AUDIT_SETUP_CODE", "open-sesame-42")
    with db.SessionLocal() as s:
        s.query(db.User).delete()
        s.commit()
    c = TestClient(app)
    assert c.get("/api/auth/status").json()["setup_code_required"] is True
    body = {"email": "boss@firm.com", "name": "Boss", "password": "a long password"}
    for code in ("", "wrong"):
        r = c.post("/api/auth/setup", json={**body, "setup_code": code})
        assert r.status_code == 403 and "setup code" in r.json()["detail"]
    assert c.post("/api/auth/setup", json={**body, "setup_code": " open-sesame-42 "}).status_code == 201
    assert TestClient(app).get("/api/auth/status").json()["setup_code_required"] is False


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


def join(a, eid, *members):
    """As admin `a`, add the signed-in users `members` to the engagement's team."""
    ids = [m.get("/api/auth/me").json()["id"] for m in members]
    current = [u["id"] for u in a.get(f"/api/engagements/{eid}").json()["team"]]
    r = a.put(f"/api/engagements/{eid}/team", json={"user_ids": sorted(set(current + ids))})
    assert r.status_code == 200, r.text


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
    join(a, eid, prep, partner)
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
    join(a, eid, prep)
    prep.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})
    prep.get("/api/runs/1/report")
    log = [x for x in prep.get("/api/activity", params={"engagement_id": eid}).json()
           if x["action"] != "engagement.team"]
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


def test_activity_survives_engagement_deletion_without_mixing_ids():
    a = admin()
    eid, _ = setup_engagement(a)
    a.delete(f"/api/engagements/{eid}")
    cl = a.get("/api/clients").json()[0]
    new = a.post(f"/api/clients/{cl['id']}/engagements",
                 json={"name": "FY2027", "period_end": "2027-12-31"}).json()
    assert new["id"] != eid  # ids are not reused
    log = a.get("/api/activity", params={"engagement_id": new["id"]}).json()
    assert [x["action"] for x in log] == ["engagement.create"]


def test_downloads_are_logged_with_fingerprints_and_settings():
    a = admin()
    eid, uid = setup_engagement(a)
    run = a.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid}).json()
    a.get(f"/api/runs/{run['id']}/report")
    s = a.post(f"/api/engagements/{eid}/samples",
               json={"upload_id": uid, "method": "random", "sample_size": 3, "seed": 7}).json()
    a.get(f"/api/samples/{s['id']}/export")
    prior = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "prior_tb"},
                   files={"file": ("prior.csv", b"Account,Description,Amount\n1000,Cash,5\n")})
    tb = a.post(f"/api/engagements/{eid}/uploads", data={"kind": "tb"},
                files={"file": ("tb.csv", b"Account,Description,Amount\n1000,Cash,9\n")})
    cmp = a.post(f"/api/engagements/{eid}/comparisons",
                 json={"current_upload_id": tb.json()["id"], "prior_upload_id": prior.json()["id"],
                       "threshold_amount": 1}).json()
    a.get(f"/api/comparisons/{cmp['id']}/report")
    f = a.post(f"/api/engagements/{eid}/findings", json={"detail": "x"}).json()
    ev = a.post(f"/api/findings/{f['id']}/evidence",
                files={"file": ("a.txt", b"abc")}).json()["attachments"][0]
    a.get(f"/api/evidence/{ev['id']}")
    log = {x["action"]: x["details"] for x in a.get("/api/activity",
                                                    params={"engagement_id": eid}).json()}
    assert len(log["run.download"]["sha256"]) == 64 and "cutoff_days" in log["run.download"]["settings"]
    assert log["sample.download"]["seed"] == 7 and log["sample.download"]["params"]
    assert len(log["sample.download"]["sha256"]) == 64
    assert log["comparison.download"]["current_sha256"] != log["comparison.download"]["prior_sha256"]
    assert "threshold_amount" in log["comparison.download"]["settings"]
    assert log["evidence.download"]["sha256"] == ev["sha256"] and log["evidence.download"]["size"] == 3


# --- Concurrent requests ------------------------------------------------------


def at_once(n, fn):
    """Run fn(i) on n threads released together; return the results."""
    barrier = threading.Barrier(n)

    def go(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(go, range(n)))


def test_concurrent_first_run_setup_creates_one_admin():
    with db.SessionLocal() as s:
        s.query(db.User).delete()
        s.commit()
    codes = at_once(6, lambda i: TestClient(app).post("/api/auth/setup", json={
        "email": f"boss{i}@firm.com", "name": "Boss", "password": "a long password"}).status_code)
    assert sorted(codes) == [201] + [409] * 5
    with db.SessionLocal() as s:
        assert s.query(db.User).count() == 1


def test_concurrent_guesses_cannot_pass_the_lockout():
    codes = at_once(12, lambda i: TestClient(app).post("/api/auth/login", json={
        "email": ADMIN["email"], "password": f"wrong {i}"}).status_code)
    assert codes.count(401) == auth.MAX_FAILED_LOGINS and codes.count(429) == 12 - 5
    with db.SessionLocal() as s:
        assert auth.recent_failed_logins(s, ADMIN["email"]) == auth.MAX_FAILED_LOGINS


def test_two_admins_demoting_each_other_keep_one_admin(monkeypatch):
    a = admin()
    other = add_user(a, "admin", "second@example.com")
    ids = {u["email"]: u["id"] for u in a.get("/api/users").json()}
    real_count = accounts.active_admins

    def slow_count(session):  # widen the window between the check and the change
        n = real_count(session)
        time.sleep(0.3)
        return n

    monkeypatch.setattr(accounts, "active_admins", slow_count)
    pairs = [(a, ids["second@example.com"]), (other, ids[ADMIN["email"]])]
    codes = at_once(2, lambda i: pairs[i][0].patch(f"/api/users/{pairs[i][1]}",
                                                   json={"role": "partner"}).status_code)
    assert sorted(codes) == [200, 400]
    with db.SessionLocal() as s:
        assert accounts.active_admins(s) == 1


def test_only_databases_with_atomic_guards_are_allowed():
    for url in ("mysql://u:p@localhost/audit", "mssql+pyodbc://u:p@dsn", "oracle://u:p@h/db"):
        with pytest.raises(RuntimeError, match="SQLite and PostgreSQL only"):
            db._make_engine(url)
    assert db._make_engine("sqlite://").dialect.name == "sqlite"


def test_adjusting_entry_permissions():
    a = admin()
    prep = add_user(a, "preparer")
    eid, _ = setup_engagement(a)
    join(a, eid, prep)
    entry = {"kind": "proposed", "description": "Cut-off",
             "lines": [{"account": "4000", "debit": 10}, {"account": "1200", "credit": 10}]}
    r = prep.post(f"/api/engagements/{eid}/adjusting-entries", json=entry)
    assert r.status_code == 201 and r.json()["author"] == "Preparer"
    assert prep.put(f"/api/engagements/{eid}/mapping",
                    json={"mappings": {"4000": "revenue"}}).status_code == 200
    booked = prep.post(f"/api/engagements/{eid}/adjusting-entries",
                       json={**entry, "kind": "adjusting"})
    assert booked.status_code == 403  # booking needs a reviewer, however it's done
    eid_ = r.json()["id"]
    assert prep.patch(f"/api/adjusting-entries/{eid_}", json={"kind": "adjusting"}).status_code == 403
    assert prep.delete(f"/api/adjusting-entries/{eid_}").status_code == 403
    assert a.patch(f"/api/adjusting-entries/{eid_}", json={"kind": "adjusting"}).status_code == 200
