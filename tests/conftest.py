import pytest

from app import db


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    """Give every test its own empty SQLite database."""
    db.configure(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    db.engine.dispose()
