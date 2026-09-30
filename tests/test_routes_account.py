import datetime
import logging

import pytest
from fastapi.testclient import TestClient
from icalendar import Calendar

import app.main as main
from app.audiobookshelf import ABSError
from app.models import Subscription, User, UserBookStatus
from app.prowlarr import ProwlarrError

from conftest import DEFAULT_PASSWORD


def _reload(session, user):
    session.expire_all()
    return session.get(User, user.id)


# ---------------------------------------------------------------- misc / static


def test_unhandled_exception_handler_returns_500_and_logs(caplog):
    @main.app.get("/__boom")
    def boom():
        raise RuntimeError("kaboom")

    try:
        with TestClient(main.app, raise_server_exceptions=False) as c:
            with caplog.at_level(logging.ERROR, logger="app.main"):
                r = c.get("/__boom")
    finally:
        main.app.router.routes[:] = [r for r in main.app.router.routes if getattr(r, "path", "") != "/__boom"]
    assert r.status_code == 500
    assert "Something went wrong" in r.text
    rec = [x for x in caplog.records if "Unhandled exception" in x.getMessage()]
    assert rec and "/__boom" in rec[0].getMessage() and rec[0].exc_info


def test_log_unhandled_exception_direct():
    class Req:
        method = "GET"

        class url:
            path = "/x"

    resp = main._log_unhandled_exception(Req(), ValueError("x"))
    assert resp.status_code == 500
    assert b"Something went wrong" in resp.body


def test_manifest(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert isinstance(r.json(), dict)


def test_service_worker(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")


def test_signup_open(session, make_user):
    assert main._signup_open(session) is True
    make_user("a")
    assert main._signup_open(session) is False


# ---------------------------------------------------------------- login


def test_login_form_renders(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_login_form_signup_link_only_when_open(client, make_user):
    assert "/signup" in client.get("/login").text
    make_user("a")
    assert "/signup" not in client.get("/login").text


def test_login_form_redirects_when_logged_in(auth_client):
    r = auth_client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_success_sets_session_and_last_login(client, make_user, session):
    user = make_user("bob")
    assert user.last_login is None
    r = client.post("/login", data={"username": "bob", "password": DEFAULT_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert _reload(session, user).last_login is not None
    assert client.get("/account/password").status_code == 200


def test_login_wrong_password(client, make_user, session):
    user = make_user("bob")
    r = client.post("/login", data={"username": "bob", "password": "nope"})
    assert r.status_code == 200
    assert "Incorrect username or password." in r.text
    assert _reload(session, user).last_login is None
    assert client.get("/account/password").status_code == 303


def test_login_unknown_user(client):
    r = client.post("/login", data={"username": "ghost", "password": "whatever1"})
    assert r.status_code == 200
    assert "Incorrect username or password." in r.text


def test_login_missing_fields(client):
    assert client.post("/login", data={"username": "x"}).status_code == 422


# ---------------------------------------------------------------- signup


def test_signup_form_open(client):
    r = client.get("/signup")
    assert r.status_code == 200


def test_signup_form_closed_redirects_to_login(client, make_user):
    make_user("a")
    r = client.get("/signup")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_signup_form_logged_in_redirects_home(auth_client):
    r = auth_client.get("/signup")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_signup_first_user_becomes_admin_and_claims_series(client, session, make_series, make_user):
    s1 = make_series("Unclaimed")
    r = client.post("/signup", data={"username": "  first  ", "password": "longenough"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    user = session.query(User).one()
    assert user.username == "first"
    assert user.is_admin is True
    assert user.last_login is not None
    assert [x.series_id for x in session.query(Subscription).filter_by(user_id=user.id)] == [s1.id]
    # logged in
    assert client.get("/account/password").status_code == 200


def test_signup_closed_redirects_and_creates_nothing(client, make_user, session):
    make_user("a")
    r = client.post("/signup", data={"username": "new", "password": "longenough"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert session.query(User).count() == 1


def test_signup_blank_username(client, session):
    r = client.post("/signup", data={"username": "   ", "password": "longenough"})
    assert r.status_code == 200
    assert "Username and password are required." in r.text
    assert session.query(User).count() == 0


def test_signup_empty_password(client, session):
    r = client.post("/signup", data={"username": "u", "password": ""})
    assert r.status_code == 200
    assert "Username and password are required." in r.text
    assert session.query(User).count() == 0


def test_signup_short_password(client, session):
    r = client.post("/signup", data={"username": "u", "password": "short"})
    assert "Password must be at least 8 characters." in r.text
    assert session.query(User).count() == 0


def test_signup_missing_field(client):
    assert client.post("/signup", data={"username": "u"}).status_code == 422


def test_signup_duplicate_username_impossible_since_closed(client, make_user):
    make_user("dup")
    r = client.post("/signup", data={"username": "dup", "password": "longenough"})
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------- logout


def test_logout_clears_session(auth_client):
    assert auth_client.get("/account/password").status_code == 200
    r = auth_client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert auth_client.get("/account/password").status_code == 303


def test_logout_when_anonymous(client):
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------- password


def test_password_form(auth_client):
    r = auth_client.get("/account/password")
    assert r.status_code == 200


def test_password_success(auth_client, session, client):
    r = auth_client.post(
        "/account/password",
        data={"current_password": DEFAULT_PASSWORD, "new_password": "brandnew123", "confirm_password": "brandnew123"},
    )
    assert r.status_code == 200
    from app.auth import verify_password

    assert verify_password("brandnew123", _reload(session, client.user).password_hash)
    assert not verify_password(DEFAULT_PASSWORD, _reload(session, client.user).password_hash)


def test_password_wrong_current(auth_client, session):
    old = auth_client.user.password_hash
    r = auth_client.post(
        "/account/password",
        data={"current_password": "wrong", "new_password": "brandnew123", "confirm_password": "brandnew123"},
    )
    assert "Current password is incorrect." in r.text
    assert _reload(session, auth_client.user).password_hash == old


def test_password_too_short(auth_client, session):
    old = auth_client.user.password_hash
    r = auth_client.post(
        "/account/password",
        data={"current_password": DEFAULT_PASSWORD, "new_password": "short", "confirm_password": "short"},
    )
    assert "New password must be at least 8 characters." in r.text
    assert _reload(session, auth_client.user).password_hash == old


def test_password_mismatch(auth_client, session):
    old = auth_client.user.password_hash
    r = auth_client.post(
        "/account/password",
        data={"current_password": DEFAULT_PASSWORD, "new_password": "brandnew123", "confirm_password": "different123"},
    )
    assert "confirmation don" in r.text and "match" in r.text
    assert _reload(session, auth_client.user).password_hash == old


# ---------------------------------------------------------------- export csv


def test_export_csv(auth_client, make_series, make_book, subscribe, today):
    a = make_series("Alpha")
    b = make_series("Beta", ended=True)
    unsub = make_series("Zeta")
    make_book(unsub, "Hidden")
    make_book(a, "A1", 1, release_date=today - datetime.timedelta(days=30))
    make_book(a, "A2", 2, release_date=today - datetime.timedelta(days=5))
    make_book(a, "A3", 3, release_date=today + datetime.timedelta(days=10))
    make_book(b, "B1", 1, release_date=today - datetime.timedelta(days=100))
    subscribe(auth_client.user, a)
    subscribe(auth_client.user, b)
    r = auth_client.get("/account/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "audiobook-subscriptions.csv" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0] == "Series,Status,URL,Latest Released,Next Book,Next Release Date"
    assert len(lines) == 3
    assert lines[1].startswith("Alpha,Ongoing,")
    assert lines[1].endswith(f",A2,A3,{(today + datetime.timedelta(days=10)).isoformat()}")
    assert lines[2].startswith("Beta,Ended,") and lines[2].endswith(",B1,,")
    assert "Hidden" not in r.text


def test_export_csv_empty(auth_client):
    r = auth_client.get("/account/export.csv")
    assert r.text.strip() == "Series,Status,URL,Latest Released,Next Book,Next Release Date"


def test_export_csv_upcoming_without_date(auth_client, make_series, make_book, subscribe):
    s = make_series("S")
    make_book(s, "TBA", 1, release_date=None)
    subscribe(auth_client.user, s)
    assert auth_client.get("/account/export.csv").text.strip().splitlines()[1].endswith(",,TBA,")


# ---------------------------------------------------------------- calendar


def test_calendar_invalid_token(client):
    assert client.get("/calendar/nope.ics").status_code == 404


def test_calendar_feed(client, make_user, make_series, make_book, subscribe, today):
    user = make_user("cal")
    s = make_series("Cal Series")
    muted = make_series("Muted Series")
    other = make_series("Other Series")
    d = today + datetime.timedelta(days=7)
    b1 = make_book(s, "Dated", 1, release_date=d)
    make_book(s, "Undated", 2, release_date=None)
    make_book(muted, "MutedBook", 1, release_date=d)
    make_book(other, "OtherBook", 1, release_date=d)
    subscribe(user, s)
    subscribe(user, muted, muted=True)
    r = client.get(f"/calendar/{user.calendar_token}.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    cal = Calendar.from_ical(r.content)
    assert str(cal["x-wr-calname"]) == "Audiobook Releases"
    events = [c for c in cal.walk() if c.name == "VEVENT"]
    assert len(events) == 1
    ev = events[0]
    assert str(ev["summary"]) == "Cal Series: Dated"
    assert ev["dtstart"].dt == d
    assert ev["dtend"].dt == d + datetime.timedelta(days=1)
    assert str(ev["uid"]) == f"book-{b1.id}@audiobook-tracker"
    assert str(ev["url"]) == b1.url


def test_calendar_feed_empty(client, make_user):
    user = make_user("cal")
    r = client.get(f"/calendar/{user.calendar_token}.ics")
    assert r.status_code == 200
    assert not [c for c in Calendar.from_ical(r.content).walk() if c.name == "VEVENT"]


# ---------------------------------------------------------------- toggles


def test_toggle_digest(auth_client, session):
    r = auth_client.post("/account/digest/toggle")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert _reload(session, auth_client.user).digest_enabled is True
    r = auth_client.post("/account/digest/toggle", headers={"referer": "/somewhere"})
    assert r.headers["location"] == "/somewhere"
    assert _reload(session, auth_client.user).digest_enabled is False


def test_regenerate_calendar_token(auth_client, session):
    old = auth_client.user.calendar_token
    r = auth_client.post("/account/calendar-token/regenerate", headers={"referer": "/back"})
    assert r.status_code == 303 and r.headers["location"] == "/back"
    new = _reload(session, auth_client.user).calendar_token
    assert new and new != old
    assert auth_client.get(f"/calendar/{old}.ics").status_code == 404
    assert auth_client.get(f"/calendar/{new}.ics").status_code == 200


def test_regenerate_calendar_token_default_redirect(auth_client):
    r = auth_client.post("/account/calendar-token/regenerate")
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------- _safe_return_to


@pytest.mark.parametrize(
    "url,default,expected",
    [
        (None, None, None),
        ("", "/d", "/d"),
        (None, "/d", "/d"),
        ("/account/integrations", None, "/account/integrations"),
        ("  /padded  ", None, "/padded"),
        ("/path?x=1&y=2", "/d", "/path?x=1&y=2"),
        ("//evil.com", "/d", "/d"),
        ("/\\evil.com", "/d", "/d"),
        ("https://evil.com", "/d", "/d"),
        ("http://evil.com/x", None, None),
        ("javascript:alert(1)", "/d", "/d"),
        ("relative/path", "/d", "/d"),
        ("   ", "/d", "/d"),
    ],
)
def test_safe_return_to(url, default, expected):
    assert main._safe_return_to(url, default) == expected


# ---------------------------------------------------------------- _json_or_redirect


class _FakeReq:
    def __init__(self, headers):
        self.headers = headers


def test_json_or_redirect_json():
    r = main._json_or_redirect(_FakeReq({"accept": "application/json"}), {"ok": True}, "/x", status_code=202)
    assert r.status_code == 202
    assert r.body == b'{"ok":true}'


def test_json_or_redirect_redirect():
    r = main._json_or_redirect(_FakeReq({}), {"ok": True}, "/x")
    assert r.status_code == 303 and r.headers["location"] == "/x"


def test_json_or_redirect_unsafe_falls_back_to_root():
    r = main._json_or_redirect(_FakeReq({"accept": "text/html"}), {}, "https://evil.com")
    assert r.status_code == 303 and r.headers["location"] == "/"


# ---------------------------------------------------------------- integrations


class FakeABS:
    libraries = [{"id": "lib1", "name": "Main"}]
    error = None
    calls = []

    def __init__(self, url, key):
        FakeABS.calls.append((url, key))

    def list_libraries(self):
        if FakeABS.error:
            raise FakeABS.error
        return FakeABS.libraries


class FakeProwlarr:
    error = None
    calls = []

    def __init__(self, url, key):
        FakeProwlarr.calls.append((url, key))

    def test_connection(self):
        if FakeProwlarr.error:
            raise FakeProwlarr.error


@pytest.fixture
def fakes(monkeypatch):
    FakeABS.libraries = [{"id": "lib1", "name": "Main"}]
    FakeABS.error = None
    FakeABS.calls = []
    FakeProwlarr.error = None
    FakeProwlarr.calls = []
    monkeypatch.setattr(main, "ABSClient", FakeABS)
    monkeypatch.setattr(main, "ProwlarrClient", FakeProwlarr)
    return FakeABS, FakeProwlarr


class _Ctx:
    """Capture the template context passed to integrations.html."""


@pytest.fixture
def ctx(monkeypatch):
    captured = {}
    real = main.templates.TemplateResponse

    def spy(name, context, *a, **k):
        captured["name"] = name
        captured["context"] = context
        return real(name, context, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    return captured


def _set(session, user, **kw):
    u = session.get(User, user.id)
    for k, v in kw.items():
        setattr(u, k, v)
    session.commit()


def test_integrations_context_unconfigured(session, make_user, fakes):
    user = make_user("a")
    c = main._integrations_context(object(), user, session)
    assert c["abs_libraries"] == [] and c["abs_error"] is None
    assert c["prowlarr_ok"] is None and c["prowlarr_error"] is None
    assert c["last_scanned_at"] is None
    assert c["abs_scanning"] is False and c["abs_scan_progress"] is None
    assert c["user"].id == user.id
    assert FakeABS.calls == [] and FakeProwlarr.calls == []


def test_integrations_context_configured_ok(session, make_user, fakes):
    user = make_user("a", abs_base_url="http://abs", abs_api_key="k", prowlarr_base_url="http://p", prowlarr_api_key="pk")
    c = main._integrations_context(object(), user, session)
    assert c["abs_libraries"] == FakeABS.libraries
    assert c["prowlarr_ok"] is True
    assert FakeABS.calls == [("http://abs", "k")]
    assert FakeProwlarr.calls == [("http://p", "pk")]


def test_integrations_context_errors(session, make_user, fakes):
    user = make_user("a", abs_base_url="http://abs", abs_api_key="k", prowlarr_base_url="http://p", prowlarr_api_key="pk")
    FakeABS.error = ABSError("abs down")
    FakeProwlarr.error = ProwlarrError("prowlarr down")
    c = main._integrations_context(object(), user, session)
    assert c["abs_error"] == "abs down" and c["abs_libraries"] == []
    assert c["prowlarr_error"] == "prowlarr down" and c["prowlarr_ok"] is None


def test_integrations_context_preset_errors_skip_checks(session, make_user, fakes):
    user = make_user("a", abs_base_url="http://abs", abs_api_key="k", prowlarr_base_url="http://p", prowlarr_api_key="pk")
    c = main._integrations_context(object(), user, session, abs_error="pre", prowlarr_error="pre2")
    assert c["abs_error"] == "pre" and c["prowlarr_error"] == "pre2"
    assert FakeABS.calls == [] and FakeProwlarr.calls == []


def test_integrations_context_last_scanned_and_scan_state(session, make_user, make_series, make_book, fakes, monkeypatch):
    user = make_user("a")
    book = make_book(make_series("S"))
    t = datetime.datetime(2026, 1, 2, 3, 4)
    session.add(UserBookStatus(user_id=user.id, book_id=book.id, checked_at=t))
    session.commit()
    monkeypatch.setattr(main, "is_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "get_user_scan_progress", lambda uid: {"percent": 5})
    c = main._integrations_context(object(), user, session)
    assert c["last_scanned_at"] == t
    assert c["abs_scanning"] is True and c["abs_scan_progress"] == {"percent": 5}


def test_integrations_requires_auth(client):
    for method, path, data in [
        ("get", "/account/integrations", None),
        ("post", "/account/integrations/audiobookshelf", {"abs_base_url": "x", "abs_api_key": "y"}),
        ("post", "/account/integrations/audiobookshelf/library", {"abs_library_id": "x"}),
        ("post", "/account/integrations/audiobookshelf/disconnect", None),
        ("post", "/account/integrations/prowlarr", {"prowlarr_base_url": "x", "prowlarr_api_key": "y"}),
        ("post", "/account/integrations/prowlarr/disconnect", None),
        ("post", "/account/library-status/refresh", None),
        ("get", "/account/library-status/progress", None),
        ("get", "/account/password", None),
        ("post", "/account/password", {"current_password": "a", "new_password": "b", "confirm_password": "b"}),
        ("get", "/account/export.csv", None),
        ("post", "/account/digest/toggle", None),
        ("post", "/account/calendar-token/regenerate", None),
    ]:
        r = getattr(client, method)(path, **({"data": data} if data else {}))
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_integrations_form(auth_client, fakes, ctx):
    r = auth_client.get("/account/integrations?return_to=/series/1")
    assert r.status_code == 200
    assert ctx["name"] == "integrations.html"
    assert ctx["context"]["return_to"] == "/series/1"


def test_integrations_form_unsafe_return_to(auth_client, fakes, ctx):
    auth_client.get("/account/integrations", params={"return_to": "//evil.com"})
    assert ctx["context"]["return_to"] is None


def test_integrations_form_no_return_to(auth_client, fakes, ctx):
    auth_client.get("/account/integrations")
    assert ctx["context"]["return_to"] is None


def test_save_audiobookshelf(auth_client, session, fakes, ctx):
    r = auth_client.post(
        "/account/integrations/audiobookshelf",
        data={"abs_base_url": "  http://abs.local/// ", "abs_api_key": "  key  "},
    )
    assert r.status_code == 200
    u = _reload(session, auth_client.user)
    assert u.abs_base_url == "http://abs.local"
    assert u.abs_api_key == "key"
    assert FakeABS.calls == [("http://abs.local", "key")]
    assert ctx["context"]["abs_libraries"] == FakeABS.libraries


def test_save_audiobookshelf_connection_error_shown(auth_client, fakes, ctx):
    FakeABS.error = ABSError("bad key")
    r = auth_client.post(
        "/account/integrations/audiobookshelf", data={"abs_base_url": "http://a", "abs_api_key": "k"}
    )
    assert r.status_code == 200
    assert ctx["context"]["abs_error"] == "bad key"


def test_save_audiobookshelf_library_starts_scan(auth_client, session, make_series, make_book, monkeypatch):
    book = make_book(make_series("S"))
    session.add(
        UserBookStatus(user_id=auth_client.user.id, book_id=book.id, in_library=True, checked_at=datetime.datetime(2026, 1, 1))
    )
    session.commit()
    ran = []
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: ran.append(uid))
    r = auth_client.post("/account/integrations/audiobookshelf/library", data={"abs_library_id": "lib9"})
    assert r.status_code == 303 and r.headers["location"] == "/account/integrations"
    assert _reload(session, auth_client.user).abs_library_id == "lib9"
    status = session.query(UserBookStatus).one()
    assert status.in_library is False and status.checked_at is None
    assert ran == [auth_client.user.id]


def test_save_audiobookshelf_library_same_id_keeps_status(auth_client, session, make_series, make_book, monkeypatch):
    _set(session, auth_client.user, abs_library_id="lib1")
    book = make_book(make_series("S"))
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=book.id, in_library=True))
    session.commit()
    ran = []
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: False)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: ran.append(uid))
    r = auth_client.post("/account/integrations/audiobookshelf/library", data={"abs_library_id": "lib1"})
    assert r.status_code == 303
    assert session.query(UserBookStatus).one().in_library is True
    assert ran == []


def test_disconnect_audiobookshelf(auth_client, session, make_series, make_book):
    _set(session, auth_client.user, abs_base_url="http://a", abs_api_key="k", abs_library_id="l")
    book = make_book(make_series("S"))
    session.add(
        UserBookStatus(
            user_id=auth_client.user.id, book_id=book.id, in_library=True,
            checked_at=datetime.datetime(2026, 1, 1), acknowledged=True, last_error="oops",
        )
    )
    session.commit()
    r = auth_client.post("/account/integrations/audiobookshelf/disconnect")
    assert r.status_code == 303 and r.headers["location"] == "/account/integrations"
    u = _reload(session, auth_client.user)
    assert u.abs_base_url is None and u.abs_api_key is None and u.abs_library_id is None
    st = session.query(UserBookStatus).one()
    assert st.in_library is False and st.checked_at is None
    assert st.acknowledged is True and st.last_error == "oops"


def test_save_prowlarr(auth_client, session, fakes, ctx):
    r = auth_client.post(
        "/account/integrations/prowlarr",
        data={"prowlarr_base_url": " http://prowlarr:9696/ ", "prowlarr_api_key": " pk "},
    )
    assert r.status_code == 200
    u = _reload(session, auth_client.user)
    assert u.prowlarr_base_url == "http://prowlarr:9696" and u.prowlarr_api_key == "pk"
    assert ctx["context"]["prowlarr_ok"] is True


def test_save_prowlarr_error_shown(auth_client, fakes, ctx):
    FakeProwlarr.error = ProwlarrError("unauthorized")
    auth_client.post("/account/integrations/prowlarr", data={"prowlarr_base_url": "http://p", "prowlarr_api_key": "k"})
    assert ctx["context"]["prowlarr_error"] == "unauthorized"
    assert ctx["context"]["prowlarr_ok"] is None


def test_disconnect_prowlarr(auth_client, session):
    _set(session, auth_client.user, prowlarr_base_url="http://p", prowlarr_api_key="k")
    r = auth_client.post("/account/integrations/prowlarr/disconnect")
    assert r.status_code == 303 and r.headers["location"] == "/account/integrations"
    u = _reload(session, auth_client.user)
    assert u.prowlarr_base_url is None and u.prowlarr_api_key is None


# ---------------------------------------------------------------- library status


def test_refresh_library_status_redirect_and_task(auth_client, monkeypatch):
    ran = []
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: ran.append(uid))
    r = auth_client.post("/account/library-status/refresh", headers={"referer": "/series/3"})
    assert r.status_code == 303 and r.headers["location"] == "/series/3"
    assert ran == [auth_client.user.id]


def test_refresh_library_status_default_redirect(auth_client, monkeypatch):
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: None)
    r = auth_client.post("/account/library-status/refresh")
    assert r.headers["location"] == "/account/integrations"


def test_refresh_library_status_json(auth_client, monkeypatch):
    ran = []
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: ran.append(uid))
    r = auth_client.post("/account/library-status/refresh", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "scanning": True, "started": True}
    assert ran == [auth_client.user.id]


def test_refresh_library_status_already_scanning(auth_client, monkeypatch):
    ran = []
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: False)
    monkeypatch.setattr(main, "run_scan_for_user", lambda uid: ran.append(uid))
    r = auth_client.post("/account/library-status/refresh", headers={"accept": "application/json"})
    assert r.json()["started"] is False
    assert ran == []


def test_refresh_library_status_unsafe_referer(auth_client, monkeypatch):
    monkeypatch.setattr(main, "mark_user_scanning", lambda uid: False)
    r = auth_client.post("/account/library-status/refresh", headers={"referer": "https://evil.com/"})
    assert r.headers["location"] == "/"


def test_library_status_progress_idle(auth_client, monkeypatch):
    monkeypatch.setattr(main, "is_user_scanning", lambda uid: False)
    monkeypatch.setattr(main, "get_user_scan_progress", lambda uid: None)
    r = auth_client.get("/account/library-status/progress")
    assert r.json() == {"scanning": False, "progress": None, "last_scanned_at": None}


def test_library_status_progress_active(auth_client, session, make_series, make_book, monkeypatch):
    book = make_book(make_series("S"))
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=book.id, checked_at=datetime.datetime(2026, 3, 4, 5, 6)))
    session.commit()
    monkeypatch.setattr(main, "is_user_scanning", lambda uid: True)
    monkeypatch.setattr(main, "get_user_scan_progress", lambda uid: {"percent": 40})
    r = auth_client.get("/account/library-status/progress")
    assert r.json() == {"scanning": True, "progress": {"percent": 40}, "last_scanned_at": "2026-03-04 05:06 UTC"}
