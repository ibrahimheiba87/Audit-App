import os

import pytest
from fastapi import Depends

from app import auth, db, doctext
from app.main import app

ADMIN = {"email": "admin@example.com", "name": "Test Admin", "password": "correct horse 1"}


def signed_in_admin(session=Depends(db.get_session)):
    return session.get(db.User, 1)


# Set AUDIT_TEST_DB_URL to run the tests against PostgreSQL (as CI does); each test starts it
# empty. Otherwise each test gets its own SQLite file.
TEST_DB_URL = os.environ.get("AUDIT_TEST_DB_URL", "")


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Give every test its own database with one admin, signed in for every request."""
    if TEST_DB_URL:
        db.engine.dispose()
        db.engine = db._make_engine(TEST_DB_URL)
        db.Base.metadata.drop_all(db.engine)
        db.configure(TEST_DB_URL)
    else:
        db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    with db.SessionLocal() as s:
        s.add(db.User(email=ADMIN["email"], name=ADMIN["name"], role="admin",
                      password_hash=auth.hash_password(ADMIN["password"])))
        s.commit()
    app.dependency_overrides[auth.current_user] = signed_in_admin
    app.dependency_overrides[auth.signed_in_user] = signed_in_admin
    yield
    app.dependency_overrides.clear()
    db.engine.dispose()


@pytest.fixture
def real_auth():
    """Use real sign-in (cookies) instead of the always-signed-in admin."""
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def read_files_now(monkeypatch):
    """Attached files are read for search straight away, not in a background thread."""
    monkeypatch.setattr(doctext, "IN_BACKGROUND", False)


@pytest.fixture(autouse=True)
def no_ai_calls(monkeypatch):
    """The AI assistant starts off, and no test can reach the Claude API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AUDIT_AI_MODEL", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("A test tried to call the Claude API.")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
