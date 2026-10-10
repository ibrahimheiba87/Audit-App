import pytest
from fastapi import Depends

from app import auth, db
from app.main import app

ADMIN = {"email": "admin@example.com", "name": "Test Admin", "password": "correct horse 1"}


def signed_in_admin(session=Depends(db.get_session)):
    return session.get(db.User, 1)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Give every test its own database with one admin, signed in for every request."""
    db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    with db.SessionLocal() as s:
        s.add(db.User(email=ADMIN["email"], name=ADMIN["name"], role="admin",
                      password_hash=auth.hash_password(ADMIN["password"])))
        s.commit()
    app.dependency_overrides[auth.current_user] = signed_in_admin
    yield
    app.dependency_overrides.clear()
    db.engine.dispose()


@pytest.fixture
def real_auth():
    """Use real sign-in (cookies) instead of the always-signed-in admin."""
    app.dependency_overrides.clear()
