"""Asking for an account on the sign-in page, and an admin approving or turning it down."""

import pytest
from fastapi.testclient import TestClient

from app import accounts, db
from app.main import app
from tests.conftest import ADMIN

pytestmark = pytest.mark.usefixtures("real_auth")

PW = "a long password"


def admin():
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": ADMIN["email"],
                                           "password": ADMIN["password"]}).status_code == 200
    return c


def ask(email="new@example.com", name="New Person", password=PW):
    return TestClient(app).post("/api/auth/signup",
                                json={"email": email, "name": name, "password": password})


def sign_in(email="new@example.com", password=PW):
    return TestClient(app).post("/api/auth/login", json={"email": email, "password": password})


def test_request_waits_for_approval_then_signs_in_with_the_chosen_role():
    assert ask(email=" New@Example.com ").status_code == 202
    res = sign_in()
    assert res.status_code == 403 and "waiting for an admin" in res.json()["detail"]
    assert sign_in(password="the wrong password").status_code == 401  # no hint without it

    a = admin()
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    assert a.post(f"/api/clients/{acme['id']}/engagements",
                  json={"name": "FY2026", "period_end": "2026-12-31"}).status_code == 201
    [req] = a.get("/api/signups").json()
    assert req["email"] == "new@example.com" and req["name"] == "New Person"
    assert "password" not in str(req)
    user = a.post(f"/api/signups/{req['id']}/approve", json={"role": "reviewer"}).json()
    assert user["role"] == "reviewer" and user["email"] == "new@example.com"
    assert a.get("/api/signups").json() == []

    c = TestClient(app)
    assert c.post("/api/auth/login", json={"email": "new@example.com", "password": PW}).status_code == 200
    assert c.get("/api/clients").json() == []  # only engagements they're added to
    actions = [x["action"] for x in a.get("/api/activity").json()]
    assert "auth.signup" in actions and "user.approve" in actions


def test_approve_defaults_to_preparer_and_can_make_a_client_user():
    a = admin()
    ask()
    ask(email="client@example.com")
    first, second = a.get("/api/signups").json()
    assert a.post(f"/api/signups/{first['id']}/approve", json={}).json()["role"] == "preparer"
    assert a.post(f"/api/signups/{second['id']}/approve",
                  json={"role": "client"}).status_code == 400  # needs a client
    client = a.post("/api/clients", json={"name": "Acme"}).json()
    user = a.post(f"/api/signups/{second['id']}/approve",
                  json={"role": "client", "client_id": client["id"]}).json()
    assert user["role"] == "client" and user["client_id"] == client["id"]


def test_turned_down_request_cannot_sign_in():
    a = admin()
    ask()
    [req] = a.get("/api/signups").json()
    assert a.delete(f"/api/signups/{req['id']}").status_code == 204
    assert a.delete(f"/api/signups/{req['id']}").status_code == 404
    assert a.post(f"/api/signups/{req['id']}/approve", json={}).status_code == 404
    assert sign_in().status_code == 401
    assert ask().status_code == 202  # they may ask again


def test_duplicates_and_weak_passwords_are_refused():
    assert ask(email=ADMIN["email"]).status_code == 409
    assert ask().status_code == 202
    assert ask(name="Someone Else").status_code == 409
    assert ask(email="short@example.com", password="short").status_code == 400
    assert ask(email="not-an-email").status_code == 400
    assert ask(email="blank@example.com", name="   ").status_code == 400


def test_adding_the_user_directly_clears_their_request():
    a = admin()
    ask()
    r = a.post("/api/users", json={"email": "new@example.com", "name": "New", "role": "preparer",
                                   "password": "long enough pw"})
    assert r.status_code == 201
    assert a.get("/api/signups").json() == []


def test_only_admins_see_and_handle_requests():
    a = admin()
    a.post("/api/users", json={"email": "p@example.com", "name": "P", "role": "partner",
                               "password": "long enough pw"})
    ask()
    p = TestClient(app)
    p.post("/api/auth/login", json={"email": "p@example.com", "password": "long enough pw"})
    assert p.get("/api/signups").status_code == 403
    assert p.post("/api/signups/1/approve", json={}).status_code == 403
    assert p.delete("/api/signups/1").status_code == 403
    assert TestClient(app).get("/api/signups").status_code == 401


def test_requests_are_capped(monkeypatch):
    monkeypatch.setattr(accounts, "MAX_PENDING_SIGNUPS", 2)
    assert ask(email="a@example.com").status_code == 202
    assert ask(email="b@example.com").status_code == 202
    assert ask(email="c@example.com").status_code == 429
    monkeypatch.setattr(accounts, "MAX_PENDING_SIGNUPS", 50)
    monkeypatch.setattr(accounts, "SIGNUPS_PER_HOUR", 2)
    assert ask(email="d@example.com").status_code == 429


def test_no_requests_before_setup():
    with db.SessionLocal() as s:
        s.query(db.User).delete()
        s.commit()
    assert ask().status_code == 409
