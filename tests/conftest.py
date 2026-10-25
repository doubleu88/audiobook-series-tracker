"""Shared test fixtures.

The app builds its SQLite engine, session secret and scheduler at import time,
so this module points it at a throwaway data directory and disables the
background scheduler *before* anything from ``app`` is imported.
"""
import datetime
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="abt-tests-")
os.environ["AUDIOBOOK_DATA_DIR"] = _DATA_DIR

from apscheduler.schedulers.background import BackgroundScheduler  # noqa: E402

BackgroundScheduler.start = lambda self, *args, **kwargs: None

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.db import engine, get_session, init_db  # noqa: E402
from app.models import Base, Book, Series, Subscription, User  # noqa: E402

DEFAULT_PASSWORD = "correct-horse-battery"
_HASH_CACHE: dict[str, str] = {}


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _fresh_db():
    """Every test starts with empty tables."""
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def make_user(session):
    def _make(username="alice", password=DEFAULT_PASSWORD, is_admin=False, **kwargs) -> User:
        if password not in _HASH_CACHE:
            _HASH_CACHE[password] = hash_password(password)
        user = User(username=username, password_hash=_HASH_CACHE[password], is_admin=is_admin, **kwargs)
        session.add(user)
        session.commit()
        return user

    return _make


@pytest.fixture
def make_series(session):
    counter = {"n": 0}

    def _make(name="Test Series", asin=None, **kwargs) -> Series:
        counter["n"] += 1
        asin = asin or f"B0SERIES{counter['n']:03d}"
        series = Series(asin=asin, name=name, url=f"https://www.audible.com/series/{asin}", **kwargs)
        session.add(series)
        session.commit()
        return series

    return _make


@pytest.fixture
def make_book(session):
    counter = {"n": 0}

    def _make(series, title=None, position=1.0, release_date=None, asin=None, **kwargs) -> Book:
        counter["n"] += 1
        asin = asin or f"B0BOOK{counter['n']:04d}"
        book = Book(
            series_id=series.id,
            asin=asin,
            title=title or f"Book {position:g}",
            position=position,
            release_date=release_date,
            url=f"https://www.audible.com/pd/{asin}",
            **kwargs,
        )
        session.add(book)
        session.commit()
        return book

    return _make


@pytest.fixture
def subscribe(session):
    def _subscribe(user, series, muted=False) -> Subscription:
        sub = Subscription(user_id=user.id, series_id=series.id, muted=muted)
        session.add(sub)
        session.commit()
        return sub

    return _subscribe


@pytest.fixture
def today():
    return datetime.date.today()


@pytest.fixture
def client():
    """Unauthenticated TestClient. Redirects are NOT followed, so tests can assert on them."""
    from app.main import app

    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def login(client):
    def _login(user: User, password=DEFAULT_PASSWORD):
        response = client.post("/login", data={"username": user.username, "password": password})
        assert response.status_code == 303, response.text
        return client

    return _login


@pytest.fixture
def auth_client(client, make_user, login):
    """TestClient logged in as a regular user (``client.user``)."""
    user = make_user("regular")
    login(user)
    client.user = user
    return client


@pytest.fixture
def admin_client(client, make_user, login):
    """TestClient logged in as an admin user (``client.user``)."""
    user = make_user("admin", is_admin=True)
    login(user)
    client.user = user
    return client
