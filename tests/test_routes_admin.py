import datetime
import logging

import pytest
from pydantic import ValidationError

from app import main as main_module
from app.logging_config import is_debug_enabled
from app.models import (
    Book,
    PushSubscription,
    Series,
    Subscription,
    User,
    UserBookStatus,
)

JSON = {"accept": "application/json"}


@pytest.fixture
def released(today):
    return today - datetime.timedelta(days=3)


@pytest.fixture
def setup_watch(session, make_series, make_book, subscribe, released):
    """Returns a factory subscribing a user to a series with one released book."""

    def _setup(user, name="S", muted=False, release_date=None, position=1.0):
        series = make_series(name)
        subscribe(user, series, muted=muted)
        book = make_book(series, position=position, release_date=release_date or released)
        return series, book

    return _setup


def status_for(session, user, book):
    session.expire_all()
    return session.query(UserBookStatus).filter_by(user_id=user.id, book_id=book.id).first()


# ---------------------------------------------------------------- _watchlist_query


def test_watchlist_query_only_released_nonmuted_subscribed(
    session, make_user, make_series, make_book, subscribe, today
):
    user = make_user("u")
    other = make_user("o")
    s1 = make_series("A")
    s_muted = make_series("M")
    s_other = make_series("X")
    subscribe(user, s1)
    subscribe(user, s_muted, muted=True)
    subscribe(other, s_other)
    past = today - datetime.timedelta(days=1)
    ok = make_book(s1, release_date=past)
    make_book(s1, position=2, release_date=today + datetime.timedelta(days=5))
    make_book(s1, position=3, release_date=None)
    make_book(s_muted, release_date=past)
    make_book(s_other, release_date=past)
    todays = make_book(s1, position=4, release_date=today)

    rows = main_module._watchlist_query(session, user).all()
    assert [b.id for b, _ in rows] == [ok.id, todays.id]
    assert all(s.id == s1.id for _, s in rows)


def test_watchlist_query_excludes_acknowledged_unless_disabled(session, make_user, setup_watch):
    user = make_user("u")
    _, b1 = setup_watch(user, "A")
    _, b2 = setup_watch(user, "B")
    session.add(UserBookStatus(user_id=user.id, book_id=b1.id, acknowledged=True))
    session.commit()

    assert [b.id for b, _ in main_module._watchlist_query(session, user).all()] == [b2.id]
    all_rows = main_module._watchlist_query(session, user, unacknowledged_only=False).all()
    assert {b.id for b, _ in all_rows} == {b1.id, b2.id}


def test_watchlist_query_unacknowledged_status_row_still_included(session, make_user, setup_watch):
    user = make_user("u")
    _, b1 = setup_watch(user)
    session.add(UserBookStatus(user_id=user.id, book_id=b1.id, acknowledged=False, in_library=True))
    session.commit()
    assert len(main_module._watchlist_query(session, user).all()) == 1


def test_watchlist_query_ack_by_other_user_does_not_hide(session, make_user, setup_watch, subscribe):
    user = make_user("u")
    other = make_user("o")
    series, b1 = setup_watch(user)
    subscribe(other, series)
    session.add(UserBookStatus(user_id=other.id, book_id=b1.id, acknowledged=True))
    session.commit()
    assert len(main_module._watchlist_query(session, user).all()) == 1
    assert main_module._watchlist_query(session, other).all() == []


def test_watchlist_query_include_status(session, make_user, setup_watch):
    user = make_user("u")
    _, b1 = setup_watch(user, "A")
    _, b2 = setup_watch(user, "B")
    session.add(UserBookStatus(user_id=user.id, book_id=b1.id, in_library=True))
    session.commit()
    rows = main_module._watchlist_query(session, user, include_status=True).all()
    assert all(len(r) == 3 for r in rows)
    by_book = {r[0].id: r[2] for r in rows}
    assert by_book[b1.id].in_library is True
    assert by_book[b2.id] is None


def test_watchlist_query_ordering(session, make_user, make_series, make_book, subscribe, today):
    user = make_user("u")
    sa = make_series("Alpha")
    sb = make_series("Beta")
    subscribe(user, sa)
    subscribe(user, sb)
    d1 = today - datetime.timedelta(days=10)
    d2 = today - datetime.timedelta(days=2)
    late = make_book(sa, position=1, release_date=d2)
    beta2 = make_book(sb, position=2, release_date=d1)
    beta1 = make_book(sb, position=1, release_date=d1)
    alpha = make_book(sa, position=5, release_date=d1)
    rows = main_module._watchlist_query(session, user).all()
    assert [b.id for b, _ in rows] == [alpha.id, beta1.id, beta2.id, late.id]


# ---------------------------------------------------------------- /watchlist


def test_watchlist_requires_login(client):
    r = client.get("/watchlist")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_watchlist_renders_entries(auth_client, setup_watch):
    setup_watch(auth_client.user, "Visible Series")
    r = auth_client.get("/watchlist")
    assert r.status_code == 200
    assert "Visible Series" in r.text


def test_watchlist_empty(auth_client):
    assert auth_client.get("/watchlist").status_code == 200


def test_watchlist_series_id_redirects(auth_client):
    r = auth_client.get("/watchlist?series_id=42")
    assert r.status_code == 302
    assert r.headers["location"] == "/series/42"


def test_watchlist_non_digit_series_id_ignored(auth_client):
    assert auth_client.get("/watchlist?series_id=abc").status_code == 200


def test_watchlist_context_flags(auth_client, session, setup_watch, monkeypatch):
    user = auth_client.user
    _, b1 = setup_watch(user, "A")
    _, b2 = setup_watch(user, "B")
    session.add(
        UserBookStatus(
            user_id=user.id,
            book_id=b1.id,
            acknowledged=True,
            in_library=True,
            checked_at=datetime.datetime.utcnow(),
        )
    )
    session.commit()
    captured = {}
    real = main_module.templates.TemplateResponse

    def spy(name, ctx, *a, **k):
        captured.update(ctx)
        return real(name, ctx, *a, **k)

    monkeypatch.setattr(main_module.templates, "TemplateResponse", spy)
    assert auth_client.get("/watchlist").status_code == 200
    entries = {e["book"].id: e for e in captured["entries"]}
    assert entries[b1.id]["acknowledged"] and entries[b1.id]["in_library"] and entries[b1.id]["library_checked"]
    assert not entries[b2.id]["acknowledged"] and not entries[b2.id]["library_checked"]
    assert captured["unacknowledged_count"] == 1
    assert captured["abs_connected"] is False


def test_watchlist_abs_connected(auth_client, session, monkeypatch):
    u = session.get(User, auth_client.user.id)
    u.abs_base_url = "http://abs"
    u.abs_library_id = "lib"
    session.commit()
    captured = {}
    real = main_module.templates.TemplateResponse
    monkeypatch.setattr(
        main_module.templates,
        "TemplateResponse",
        lambda name, ctx, *a, **k: (captured.update(ctx), real(name, ctx, *a, **k))[1],
    )
    auth_client.get("/watchlist")
    assert captured["abs_connected"] is True


# ---------------------------------------------------------------- _acknowledge_book / _unacknowledge_book


def test_acknowledge_book_helper_creates_row(session, make_user, setup_watch):
    user = make_user("u")
    _, book = setup_watch(user)
    main_module._acknowledge_book(session, user, book)
    session.commit()
    st = status_for(session, user, book)
    assert st.acknowledged is True and st.acknowledged_at is not None


def test_acknowledge_book_helper_updates_existing_preserving_fields(session, make_user, setup_watch):
    user = make_user("u")
    _, book = setup_watch(user)
    session.add(UserBookStatus(user_id=user.id, book_id=book.id, in_library=True, acknowledged=False))
    session.commit()
    main_module._acknowledge_book(session, user, book)
    main_module._acknowledge_book(session, user, book)  # idempotent
    session.commit()
    st = status_for(session, user, book)
    assert st.acknowledged is True and st.in_library is True
    assert session.query(UserBookStatus).count() == 1


def test_unacknowledge_book_helper_clears(session, make_user, setup_watch):
    user = make_user("u")
    _, book = setup_watch(user)
    main_module._acknowledge_book(session, user, book)
    session.commit()
    main_module._unacknowledge_book(session, user, book)
    session.commit()
    st = status_for(session, user, book)
    assert st.acknowledged is False and st.acknowledged_at is None


def test_unacknowledge_book_helper_no_row_is_noop(session, make_user, setup_watch):
    user = make_user("u")
    _, book = setup_watch(user)
    main_module._unacknowledge_book(session, user, book)
    session.commit()
    assert session.query(UserBookStatus).count() == 0


# ---------------------------------------------------------------- acknowledge / unacknowledge routes


def test_acknowledge_requires_login(client):
    r = client.post("/books/1/acknowledge")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/books/1/unacknowledge")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_acknowledge_redirects_default(auth_client, session, setup_watch):
    _, book = setup_watch(auth_client.user)
    r = auth_client.post(f"/books/{book.id}/acknowledge")
    assert r.status_code == 303 and r.headers["location"] == "/watchlist"
    assert status_for(session, auth_client.user, book).acknowledged is True


def test_acknowledge_json(auth_client, session, setup_watch):
    _, book = setup_watch(auth_client.user)
    r = auth_client.post(f"/books/{book.id}/acknowledge", headers=JSON)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "book_id": book.id, "acknowledged": True}


def test_acknowledge_return_to_form_and_query(auth_client, setup_watch):
    _, book = setup_watch(auth_client.user)
    r = auth_client.post(f"/books/{book.id}/acknowledge", data={"return_to": "/series/1"})
    assert r.headers["location"] == "/series/1"
    r = auth_client.post(f"/books/{book.id}/acknowledge?return_to=/foo")
    assert r.headers["location"] == "/foo"


@pytest.mark.parametrize("bad", ["//evil.com", "http://evil.com", "/\\evil.com", "javascript:x"])
def test_acknowledge_unsafe_return_to_falls_back(auth_client, setup_watch, bad):
    _, book = setup_watch(auth_client.user)
    r = auth_client.post(f"/books/{book.id}/acknowledge", data={"return_to": bad})
    assert r.headers["location"] == "/watchlist"


def test_acknowledge_missing_book_json_404(auth_client):
    r = auth_client.post("/books/9999/acknowledge", headers=JSON)
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_acknowledge_missing_book_redirects(auth_client):
    r = auth_client.post("/books/9999/acknowledge")
    assert r.status_code == 303 and r.headers["location"] == "/watchlist"


def test_acknowledge_not_subscribed_denied(auth_client, session, make_series, make_book):
    book = make_book(make_series("Nope"), release_date=datetime.date(2020, 1, 1))
    r = auth_client.post(f"/books/{book.id}/acknowledge", headers=JSON)
    assert r.status_code == 404
    assert session.query(UserBookStatus).count() == 0


def test_unacknowledge_roundtrip(auth_client, session, setup_watch):
    _, book = setup_watch(auth_client.user)
    auth_client.post(f"/books/{book.id}/acknowledge")
    r = auth_client.post(f"/books/{book.id}/unacknowledge", headers=JSON)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "book_id": book.id, "acknowledged": False}
    assert status_for(session, auth_client.user, book).acknowledged is False


def test_unacknowledge_redirect_and_return_to(auth_client, setup_watch):
    _, book = setup_watch(auth_client.user)
    r = auth_client.post(f"/books/{book.id}/unacknowledge")
    assert r.status_code == 303 and r.headers["location"] == "/watchlist"
    r = auth_client.post(f"/books/{book.id}/unacknowledge", data={"return_to": "/x"})
    assert r.headers["location"] == "/x"
    r = auth_client.post(f"/books/{book.id}/unacknowledge", data={"return_to": "//evil"})
    assert r.headers["location"] == "/watchlist"


def test_unacknowledge_missing_and_unsubscribed(auth_client, make_series, make_book):
    r = auth_client.post("/books/9999/unacknowledge", headers=JSON)
    assert r.status_code == 404
    r = auth_client.post("/books/9999/unacknowledge")
    assert r.status_code == 303
    book = make_book(make_series("Nope"), release_date=datetime.date(2020, 1, 1))
    r = auth_client.post(f"/books/{book.id}/unacknowledge", headers=JSON)
    assert r.status_code == 404


# ---------------------------------------------------------------- /watchlist/acknowledge-all


@pytest.fixture
def three_books(auth_client, session, setup_watch):
    user = auth_client.user
    books = [setup_watch(user, f"S{i}")[1] for i in range(3)]
    session.add(UserBookStatus(user_id=user.id, book_id=books[0].id, in_library=True))
    session.commit()
    return books


def acked_ids(session, user):
    session.expire_all()
    return {
        s.book_id
        for s in session.query(UserBookStatus).filter_by(user_id=user.id, acknowledged=True).all()
    }


def test_acknowledge_all_requires_login(client):
    r = client.post("/watchlist/acknowledge-all")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_acknowledge_all_everything(auth_client, session, three_books):
    r = auth_client.post("/watchlist/acknowledge-all", headers=JSON)
    assert r.status_code == 200
    assert r.json()["count"] == 3
    assert set(r.json()["acknowledged_ids"]) == {b.id for b in three_books}
    assert acked_ids(session, auth_client.user) == {b.id for b in three_books}


def test_acknowledge_all_redirect(auth_client, three_books):
    r = auth_client.post("/watchlist/acknowledge-all")
    assert r.status_code == 303 and r.headers["location"] == "/watchlist"


def test_acknowledge_all_book_ids(auth_client, session, three_books):
    ids = f"{three_books[1].id}, {three_books[2].id},abc,,"
    r = auth_client.post("/watchlist/acknowledge-all", data={"book_ids": ids}, headers=JSON)
    assert set(r.json()["acknowledged_ids"]) == {three_books[1].id, three_books[2].id}
    assert acked_ids(session, auth_client.user) == {three_books[1].id, three_books[2].id}


def test_acknowledge_all_book_ids_ignores_unwatched(auth_client, session, three_books, make_series, make_book):
    foreign = make_book(make_series("F"), release_date=datetime.date(2020, 1, 1))
    r = auth_client.post("/watchlist/acknowledge-all", data={"book_ids": str(foreign.id)}, headers=JSON)
    assert r.json() == {"ok": True, "acknowledged_ids": [], "count": 0}


@pytest.mark.parametrize("flt", ["in_library", "in_library_unacknowledged"])
def test_acknowledge_all_in_library(auth_client, session, three_books, flt):
    r = auth_client.post("/watchlist/acknowledge-all", data={"library_filter": flt}, headers=JSON)
    assert r.json()["acknowledged_ids"] == [three_books[0].id]


def test_acknowledge_all_not_in_library(auth_client, session, three_books):
    r = auth_client.post("/watchlist/acknowledge-all", data={"library_filter": "not_in_library"}, headers=JSON)
    assert set(r.json()["acknowledged_ids"]) == {three_books[1].id, three_books[2].id}
    assert three_books[0].id not in acked_ids(session, auth_client.user)


@pytest.mark.parametrize("flt", ["not_in_library_acknowledged", "missing_acknowledged"])
def test_acknowledge_all_acknowledged_filters_are_noop(auth_client, session, three_books, flt):
    r = auth_client.post("/watchlist/acknowledge-all", data={"library_filter": flt}, headers=JSON)
    assert r.json()["count"] == 0
    assert acked_ids(session, auth_client.user) == set()


def test_acknowledge_all_unknown_filter_acks_all(auth_client, three_books):
    r = auth_client.post("/watchlist/acknowledge-all", data={"library_filter": "bogus"}, headers=JSON)
    assert r.json()["count"] == 3


def test_acknowledge_all_book_ids_beat_library_filter(auth_client, three_books):
    r = auth_client.post(
        "/watchlist/acknowledge-all",
        data={"book_ids": str(three_books[1].id), "library_filter": "in_library"},
        headers=JSON,
    )
    assert r.json()["acknowledged_ids"] == [three_books[1].id]


def test_acknowledge_all_skips_already_acknowledged(auth_client, session, three_books):
    auth_client.post(f"/books/{three_books[0].id}/acknowledge")
    r = auth_client.post("/watchlist/acknowledge-all", headers=JSON)
    assert set(r.json()["acknowledged_ids"]) == {three_books[1].id, three_books[2].id}


def test_acknowledge_all_empty_watchlist(auth_client):
    r = auth_client.post("/watchlist/acknowledge-all", headers=JSON)
    assert r.json() == {"ok": True, "acknowledged_ids": [], "count": 0}


# ---------------------------------------------------------------- admin access control


ADMIN_GETS = ["/admin/health", "/admin/users"]
ADMIN_POSTS = [
    "/admin/debug-logging/toggle",
    "/admin/users",
    "/admin/users/1/toggle-admin",
    "/admin/users/1/delete",
]


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_get_unauthenticated_redirects(client, path):
    r = client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_admin_post_unauthenticated_redirects(client, path):
    r = client.post(path, data={"username": "x", "password": "longenough"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_admin_get_non_admin_404(auth_client, path):
    assert auth_client.get(path).status_code == 404


@pytest.mark.parametrize("path", ADMIN_POSTS)
def test_admin_post_non_admin_404(auth_client, path):
    r = auth_client.post(path, data={"username": "x", "password": "longenough"})
    assert r.status_code == 404


# ---------------------------------------------------------------- /admin/health + debug toggle


def test_admin_health_lists_unhealthy_and_never_checked(admin_client, session, make_series):
    bad = make_series("Failing Series", consecutive_failures=3)
    worse = make_series("Worse Series", consecutive_failures=9)
    fine = make_series("Fine Series", consecutive_failures=0, last_checked=datetime.datetime.utcnow())
    fresh = make_series("Never Series")
    captured = {}
    real = main_module.templates.TemplateResponse
    orig = main_module.templates.TemplateResponse
    main_module.templates.TemplateResponse = lambda n, c, *a, **k: (captured.update(c), real(n, c, *a, **k))[1]
    try:
        r = admin_client.get("/admin/health")
    finally:
        main_module.templates.TemplateResponse = orig
    assert r.status_code == 200
    assert [s.id for s in captured["unhealthy"]] == [worse.id, bad.id]
    never_ids = {s.id for s in captured["never_checked"]}
    assert fresh.id in never_ids and fine.id not in never_ids
    assert captured["debug_logging_enabled"] == is_debug_enabled()
    assert "Failing Series" in r.text


def test_admin_health_empty(admin_client):
    assert admin_client.get("/admin/health").status_code == 200


@pytest.fixture
def restore_log_level():
    root = logging.getLogger()
    level = root.level
    yield
    root.setLevel(level)


def test_toggle_debug_logging_flips(admin_client, restore_log_level):
    logging.getLogger().setLevel(logging.INFO)
    r = admin_client.post("/admin/debug-logging/toggle")
    assert r.status_code == 303 and r.headers["location"] == "/admin/health"
    assert is_debug_enabled() is True
    admin_client.post("/admin/debug-logging/toggle")
    assert is_debug_enabled() is False


def test_toggle_debug_logging_calls_setter(admin_client, monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "is_debug_enabled", lambda: False)
    monkeypatch.setattr(main_module, "set_debug_logging", calls.append)
    admin_client.post("/admin/debug-logging/toggle")
    assert calls == [True]
    monkeypatch.setattr(main_module, "is_debug_enabled", lambda: True)
    admin_client.post("/admin/debug-logging/toggle")
    assert calls == [True, False]


# ---------------------------------------------------------------- /admin/users


def test_admin_users_lists_users(admin_client, make_user):
    make_user("zelda_user")
    r = admin_client.get("/admin/users")
    assert r.status_code == 200
    assert "zelda_user" in r.text and "admin" in r.text


def test_create_user_success(admin_client, session):
    r = admin_client.post("/admin/users", data={"username": "  newbie  ", "password": "longenough"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/users"
    session.expire_all()
    u = session.query(User).filter_by(username="newbie").one()
    assert u.is_admin is False
    assert u.password_hash != "longenough"


def test_create_user_can_login(admin_client, client):
    admin_client.post("/admin/users", data={"username": "newbie", "password": "longenough"})
    client.cookies.clear()
    r = client.post("/login", data={"username": "newbie", "password": "longenough"})
    assert r.status_code == 303


def test_create_admin_user(admin_client, session):
    admin_client.post("/admin/users", data={"username": "boss", "password": "longenough", "is_admin": "true"})
    session.expire_all()
    assert session.query(User).filter_by(username="boss").one().is_admin is True


@pytest.mark.parametrize(
    "data,msg",
    [
        ({"username": "   ", "password": "longenough"}, "required"),
        ({"username": "bob", "password": "short"}, "at least 8"),
        ({"username": "admin", "password": "longenough"}, "already taken"),
    ],
)
def test_create_user_validation_errors(admin_client, session, data, msg):
    r = admin_client.post("/admin/users", data=data)
    assert r.status_code == 200
    assert msg in r.text
    session.expire_all()
    assert session.query(User).count() == 1


def test_create_user_empty_password_required(admin_client):
    r = admin_client.post("/admin/users", data={"username": "bob", "password": ""})
    assert r.status_code == 200 and "required" in r.text


def test_create_user_missing_fields_422(admin_client):
    assert admin_client.post("/admin/users", data={"username": "bob"}).status_code == 422


def test_create_user_password_exactly_8_ok(admin_client, session):
    r = admin_client.post("/admin/users", data={"username": "bob", "password": "12345678"})
    assert r.status_code == 303


# ---------------------------------------------------------------- toggle-admin


def test_toggle_admin_promotes_and_demotes(admin_client, session, make_user):
    other = make_user("other")
    r = admin_client.post(f"/admin/users/{other.id}/toggle-admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/users"
    session.expire_all()
    assert session.get(User, other.id).is_admin is True
    admin_client.post(f"/admin/users/{other.id}/toggle-admin")
    session.expire_all()
    assert session.get(User, other.id).is_admin is False


def test_toggle_admin_cannot_demote_last_admin(admin_client, session):
    admin_client.post(f"/admin/users/{admin_client.user.id}/toggle-admin")
    session.expire_all()
    assert session.get(User, admin_client.user.id).is_admin is True


def test_toggle_admin_can_demote_when_multiple_admins(admin_client, session, make_user):
    other = make_user("other", is_admin=True)
    admin_client.post(f"/admin/users/{other.id}/toggle-admin")
    session.expire_all()
    assert session.get(User, other.id).is_admin is False


def test_toggle_admin_unknown_user_noop(admin_client, session):
    r = admin_client.post("/admin/users/9999/toggle-admin")
    assert r.status_code == 303
    session.expire_all()
    assert session.query(User).count() == 1


# ---------------------------------------------------------------- delete user


def test_delete_user(admin_client, session, make_user):
    other_id = make_user("other").id
    r = admin_client.post(f"/admin/users/{other_id}/delete")
    assert r.status_code == 303 and r.headers["location"] == "/admin/users"
    session.expire_all()
    assert session.get(User, other_id) is None


def test_delete_self_refused(admin_client, session):
    admin_client.post(f"/admin/users/{admin_client.user.id}/delete")
    session.expire_all()
    assert session.get(User, admin_client.user.id) is not None


def test_delete_other_admin_allowed_when_multiple(admin_client, session, make_user):
    other_id = make_user("other", is_admin=True).id
    admin_client.post(f"/admin/users/{other_id}/delete")
    session.expire_all()
    assert session.get(User, other_id) is None


def test_delete_last_admin_only_possible_via_self_so_refused(admin_client, session, make_user):
    # The sole admin cannot delete themselves, so at least one admin always remains.
    make_user("other")
    admin_client.post(f"/admin/users/{admin_client.user.id}/delete")
    session.expire_all()
    assert session.query(User).filter_by(is_admin=True).count() == 1


def test_delete_unknown_user_noop(admin_client, session):
    r = admin_client.post("/admin/users/9999/delete")
    assert r.status_code == 303
    session.expire_all()
    assert session.query(User).count() == 1


@pytest.mark.xfail(
    strict=True,
    reason="User has no cascade to Subscription, so the deleted user's subscriptions remain "
    "and the 'remaining == 0' orphaned-series cleanup in admin_delete_user never fires",
)
def test_delete_user_removes_orphaned_series_only(
    admin_client, session, make_user, make_series, make_book, subscribe
):
    other = make_user("other")
    solo = make_series("Solo")
    shared = make_series("Shared")
    make_book(solo)
    subscribe(other, solo)
    subscribe(other, shared)
    subscribe(admin_client.user, shared)
    solo_id, shared_id, other_id = solo.id, shared.id, other.id

    admin_client.post(f"/admin/users/{other_id}/delete")
    session.expire_all()
    assert session.get(Series, solo_id) is None
    assert session.query(Book).count() == 0  # cascaded with the orphaned series
    assert session.get(Series, shared_id) is not None


@pytest.mark.xfail(
    strict=True, reason="deleting a user leaves their Subscription rows orphaned (no cascade)"
)
def test_delete_user_removes_subscriptions(admin_client, session, make_user, make_series, subscribe):
    other = make_user("other")
    series = make_series("S")
    subscribe(other, series)
    subscribe(admin_client.user, series)
    admin_id = admin_client.user.id
    admin_client.post(f"/admin/users/{other.id}/delete")
    session.expire_all()
    subs = session.query(Subscription).all()
    assert [s.user_id for s in subs] == [admin_id]


@pytest.mark.xfail(strict=True, reason="deleting a user leaves their PushSubscription/UserBookStatus rows orphaned")
def test_delete_user_removes_push_subscriptions(admin_client, session, make_user):
    other = make_user("other")
    session.add(PushSubscription(user_id=other.id, endpoint="e", p256dh="p", auth="a"))
    session.commit()
    admin_client.post(f"/admin/users/{other.id}/delete")
    session.expire_all()
    assert session.query(PushSubscription).count() == 0


# ---------------------------------------------------------------- push models


def test_push_subscription_in_valid():
    m = main_module.PushSubscriptionIn(endpoint="https://e", keys={"p256dh": "a", "auth": "b"})
    assert m.endpoint == "https://e" and m.keys == {"p256dh": "a", "auth": "b"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"keys": {"p256dh": "a", "auth": "b"}},
        {"endpoint": "e"},
        {"endpoint": "e", "keys": "notadict"},
        {"endpoint": "e", "keys": {"p256dh": 1}},
    ],
)
def test_push_subscription_in_invalid(kwargs):
    with pytest.raises(ValidationError):
        main_module.PushSubscriptionIn(**kwargs)


def test_push_unsubscribe_in():
    assert main_module.PushUnsubscribeIn(endpoint="e").endpoint == "e"
    with pytest.raises(ValidationError):
        main_module.PushUnsubscribeIn()


# ---------------------------------------------------------------- push routes


def test_push_routes_require_login(client):
    r = client.get("/push/vapid-public-key")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/push/subscribe", json={"endpoint": "e", "keys": {"p256dh": "a", "auth": "b"}})
    assert r.status_code == 303
    r = client.post("/push/unsubscribe", json={"endpoint": "e"})
    assert r.status_code == 303


def test_vapid_public_key(auth_client, monkeypatch):
    monkeypatch.setattr(main_module, "get_vapid_public_key_b64", lambda: "PUBKEY")
    r = auth_client.get("/push/vapid-public-key")
    assert r.status_code == 200 and r.json() == {"key": "PUBKEY"}


def test_vapid_public_key_real_generation(auth_client):
    key = auth_client.get("/push/vapid-public-key").json()["key"]
    assert isinstance(key, str) and len(key) >= 80
    assert auth_client.get("/push/vapid-public-key").json()["key"] == key


def sub_body(endpoint="https://push/1", p="P", a="A"):
    return {"endpoint": endpoint, "keys": {"p256dh": p, "auth": a}}


def test_push_subscribe_creates(auth_client, session):
    r = auth_client.post("/push/subscribe", json=sub_body())
    assert r.status_code == 200 and r.json() == {"ok": True}
    session.expire_all()
    s = session.query(PushSubscription).one()
    assert (s.user_id, s.endpoint, s.p256dh, s.auth) == (auth_client.user.id, "https://push/1", "P", "A")


def test_push_subscribe_updates_existing_endpoint(auth_client, session, make_user):
    other = make_user("other")
    session.add(PushSubscription(user_id=other.id, endpoint="https://push/1", p256dh="old", auth="old"))
    session.commit()
    r = auth_client.post("/push/subscribe", json=sub_body(p="new", a="newer"))
    assert r.json() == {"ok": True}
    session.expire_all()
    s = session.query(PushSubscription).one()
    assert (s.user_id, s.p256dh, s.auth) == (auth_client.user.id, "new", "newer")


def test_push_subscribe_multiple_endpoints(auth_client, session):
    auth_client.post("/push/subscribe", json=sub_body("e1"))
    auth_client.post("/push/subscribe", json=sub_body("e2"))
    session.expire_all()
    assert session.query(PushSubscription).count() == 2


def test_push_subscribe_invalid_payload_422(auth_client, session):
    assert auth_client.post("/push/subscribe", json={"endpoint": "e"}).status_code == 422
    session.expire_all()
    assert session.query(PushSubscription).count() == 0


def test_push_subscribe_missing_key_names(auth_client):
    # keys dict validates as dict[str,str] but the handler indexes p256dh/auth
    with pytest.raises(KeyError):
        auth_client.post("/push/subscribe", json={"endpoint": "e", "keys": {"p256dh": "x"}})


def test_push_unsubscribe_deletes_own(auth_client, session):
    auth_client.post("/push/subscribe", json=sub_body("e1"))
    auth_client.post("/push/subscribe", json=sub_body("e2"))
    r = auth_client.post("/push/unsubscribe", json={"endpoint": "e1"})
    assert r.json() == {"ok": True}
    session.expire_all()
    assert [s.endpoint for s in session.query(PushSubscription).all()] == ["e2"]


def test_push_unsubscribe_other_users_endpoint_untouched(auth_client, session, make_user):
    other = make_user("other")
    session.add(PushSubscription(user_id=other.id, endpoint="theirs", p256dh="p", auth="a"))
    session.commit()
    r = auth_client.post("/push/unsubscribe", json={"endpoint": "theirs"})
    assert r.json() == {"ok": True}
    session.expire_all()
    assert session.query(PushSubscription).count() == 1


def test_push_unsubscribe_unknown_ok_and_invalid_422(auth_client):
    assert auth_client.post("/push/unsubscribe", json={"endpoint": "nope"}).json() == {"ok": True}
    assert auth_client.post("/push/unsubscribe", json={}).status_code == 422
