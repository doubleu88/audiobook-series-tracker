import datetime

import pytest
from sqlalchemy import create_engine, inspect, text

from app import db


def cols(conn, table):
    return {c["name"] for c in inspect(conn).get_columns(table)}


@pytest.fixture
def legacy():
    """Factory: builds a temp engine with the given DDL statements."""
    engines = []

    def _make(*ddl):
        from sqlalchemy.pool import StaticPool

        e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        engines.append(e)
        with e.begin() as conn:
            for stmt in ddl:
                conn.execute(text(stmt))
        return e

    yield _make
    for e in engines:
        e.dispose()


BOOKS_OLD = "CREATE TABLE books (id INTEGER PRIMARY KEY, series_id INTEGER, asin VARCHAR, title VARCHAR, position FLOAT, release_date DATE, url VARCHAR)"
SERIES_OLD = "CREATE TABLE series (id INTEGER PRIMARY KEY, asin VARCHAR, name VARCHAR, url VARCHAR, ended BOOLEAN, last_checked DATETIME)"
USERS_OLD = "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, password_hash VARCHAR, created_at DATETIME)"
SUBS_OLD = "CREATE TABLE subscriptions (id INTEGER PRIMARY KEY, user_id INTEGER, series_id INTEGER)"
UBS_OLD = "CREATE TABLE user_book_status (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER, in_library BOOLEAN)"


def test_migrate_books_and_series(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD, "INSERT INTO books (id, title) VALUES (1, 'old')")
    with e.begin() as conn:
        db._migrate(conn)
    with e.connect() as conn:
        assert {"cover_image", "release_day_notified", "created_at"} <= cols(conn, "books")
        assert {"consecutive_failures", "last_failure_at", "last_failure_reason"} <= cols(conn, "series")
        row = conn.execute(text("SELECT created_at, release_day_notified, cover_image FROM books")).one()
        assert row.created_at is not None  # backfilled
        assert row.release_day_notified == 0
        assert row.cover_image is None


def test_migrate_without_optional_tables(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD)
    with e.begin() as conn:
        db._migrate(conn)
        assert "users" not in inspect(conn).get_table_names()


def test_migrate_subscriptions_muted(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD, SUBS_OLD, "INSERT INTO subscriptions (user_id, series_id) VALUES (1, 1)")
    with e.begin() as conn:
        db._migrate(conn)
    with e.connect() as conn:
        assert "muted" in cols(conn, "subscriptions")
        assert conn.execute(text("SELECT muted FROM subscriptions")).scalar() == 0


def test_migrate_user_book_status(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD, UBS_OLD)
    with e.begin() as conn:
        db._migrate(conn)
    with e.connect() as conn:
        assert {"acknowledged", "acknowledged_at"} <= cols(conn, "user_book_status")


def test_migrate_users_columns_tokens_and_admin(legacy):
    e = legacy(
        BOOKS_OLD,
        SERIES_OLD,
        USERS_OLD,
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'late', 'h', '2024-05-01 00:00:00')",
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (2, 'early', 'h', '2023-01-01 00:00:00')",
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (3, 'mid', 'h', '2023-06-01 00:00:00')",
    )
    with e.begin() as conn:
        db._migrate(conn)
    with e.connect() as conn:
        assert {
            "is_admin", "last_login", "digest_enabled", "calendar_token", "abs_base_url",
            "abs_api_key", "abs_library_id", "prowlarr_base_url", "prowlarr_api_key",
        } <= cols(conn, "users")
        rows = conn.execute(text("SELECT username, is_admin, digest_enabled, calendar_token FROM users")).all()
        tokens = [r.calendar_token for r in rows]
        assert all(tokens) and len(set(tokens)) == 3
        assert {r.username: r.is_admin for r in rows} == {"late": 0, "early": 1, "mid": 0}
        assert all(r.digest_enabled == 0 for r in rows)
        indexes = {i["name"]: i for i in inspect(conn).get_indexes("users")}
        assert indexes["ix_users_calendar_token"]["unique"]


def test_migrate_admin_tiebreak_by_id(legacy):
    e = legacy(
        BOOKS_OLD,
        SERIES_OLD,
        USERS_OLD,
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (5, 'b', 'h', '2024-01-01 00:00:00')",
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (4, 'a', 'h', '2024-01-01 00:00:00')",
    )
    with e.begin() as conn:
        db._migrate(conn)
        admins = conn.execute(text("SELECT username FROM users WHERE is_admin = 1")).scalars().all()
    assert admins == ["a"]


def test_migrate_existing_admin_not_reassigned(legacy):
    e = legacy(
        BOOKS_OLD,
        SERIES_OLD,
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR, password_hash VARCHAR, created_at DATETIME, is_admin BOOLEAN)",
        "INSERT INTO users VALUES (1, 'first', 'h', '2023-01-01 00:00:00', 0)",
        "INSERT INTO users VALUES (2, 'second', 'h', '2024-01-01 00:00:00', 1)",
    )
    with e.begin() as conn:
        db._migrate(conn)
        admins = conn.execute(text("SELECT username FROM users WHERE is_admin = 1")).scalars().all()
    assert admins == ["second"]


def test_migrate_empty_users_table_no_admin(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD, USERS_OLD)
    with e.begin() as conn:
        db._migrate(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar() == 0


def test_migrate_is_idempotent(legacy):
    e = legacy(BOOKS_OLD, SERIES_OLD, USERS_OLD, SUBS_OLD, UBS_OLD,
               "INSERT INTO users (id, username, password_hash, created_at) VALUES (1, 'u', 'h', '2024-01-01 00:00:00')")
    with e.begin() as conn:
        db._migrate(conn)
        token = conn.execute(text("SELECT calendar_token FROM users")).scalar()
    with e.begin() as conn:
        db._migrate(conn)
        assert conn.execute(text("SELECT calendar_token FROM users")).scalar() == token


def test_init_db_creates_tables_and_is_repeatable():
    db.init_db()
    db.init_db()
    names = set(inspect(db.engine).get_table_names())
    assert {"users", "series", "books", "subscriptions", "push_subscriptions", "user_book_status"} <= names


def test_init_db_logs_and_reraises_migration_failure(monkeypatch, caplog):
    def boom(conn):
        raise RuntimeError("migration broke")

    monkeypatch.setattr(db, "_migrate", boom)
    with pytest.raises(RuntimeError, match="migration broke"):
        db.init_db()
    assert "Database migration failed" in caplog.text


def test_get_session_returns_independent_sessions(make_user):
    s1, s2 = db.get_session(), db.get_session()
    try:
        assert s1 is not s2
        assert s1.get_bind() is db.engine
    finally:
        s1.close()
        s2.close()


def test_get_session_expire_on_commit_disabled(make_user):
    from app.models import User

    s = db.get_session()
    try:
        u = User(username="x", password_hash="h")
        s.add(u)
        s.commit()
        assert "username" in u.__dict__  # not expired
    finally:
        s.close()
