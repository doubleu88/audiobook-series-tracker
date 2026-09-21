"""Tests for the dashboard, search, add/import, series and book-download routes in app/main.py."""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.main as main
from app.models import Book, Series, Subscription, UserBookStatus
from app.prowlarr import ProwlarrError, ProwlarrResult
from app.scraper import ScrapedBook, ScrapedSeries, SeriesPageError, SeriesSearchResult


def scraped(asin="B0NEWSER01", name="Scraped Series", books=None):
    return ScrapedSeries(
        asin=asin,
        name=name,
        url=f"https://www.audible.com/series/x/{asin}",
        books=books or [],
    )


@pytest.fixture
def fake_fetch(monkeypatch):
    """Patch app.main.fetch_series; returns a dict url -> ScrapedSeries or Exception."""
    table = {}

    def _fetch(url):
        result = table[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main, "fetch_series", _fetch)
    return table


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda s: None)


def set_prowlarr(session, user, url="http://prowlarr.local", key="k"):
    db_user = session.get(type(user), user.id)
    db_user.prowlarr_base_url = url
    db_user.prowlarr_api_key = key
    session.commit()


def sub_count(session, series_id):
    session.expire_all()
    return session.query(Subscription).filter_by(series_id=series_id).count()


# ---------------------------------------------------------------- auth

@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/"),
        ("get", "/search"),
        ("get", "/add"),
        ("post", "/add"),
        ("get", "/import"),
        ("post", "/import"),
        ("post", "/import/confirm"),
        ("post", "/series/1/refresh"),
        ("post", "/series/1/toggle-ended"),
        ("post", "/series/1/toggle-mute"),
        ("post", "/series/1/unsubscribe"),
        ("get", "/series/1"),
        ("post", "/series/1/acknowledge-in-library"),
        ("get", "/books/1/download"),
        ("post", "/books/1/download/grab"),
    ],
)
def test_routes_require_login(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code in (303, 401)
    if response.status_code == 303:
        assert response.headers["location"] == "/login"


# ---------------------------------------------------------------- dashboard

def test_dashboard_empty_state(auth_client):
    response = auth_client.get("/")
    assert response.status_code == 200


def test_dashboard_lists_subscribed_series_only(auth_client, make_series, subscribe, make_user):
    mine = make_series("My Visible Saga")
    other = make_series("Someone Elses Saga")
    subscribe(auth_client.user, mine)
    subscribe(make_user("bob"), other)
    body = auth_client.get("/").text
    assert "My Visible Saga" in body
    assert "Someone Elses Saga" not in body


@pytest.fixture
def dash_ctx(monkeypatch):
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    return captured


def _titles(entries):
    return [e["book"].title for e in entries]


def test_dashboard_recent_and_upcoming_books(auth_client, make_series, make_book, subscribe, today, dash_ctx):
    series = make_series("Dash Series")
    subscribe(auth_client.user, series)
    make_book(series, "RecentBook", 1, today - datetime.timedelta(days=5))
    make_book(series, "UpcomingBook", 2, today + datetime.timedelta(days=5))
    make_book(series, "OldBook", 3, today - datetime.timedelta(days=700))
    make_book(series, "FarFuture", 4, today + datetime.timedelta(days=700))
    make_book(series, "NoDate", 5, None)
    assert auth_client.get("/").status_code == 200
    assert _titles(dash_ctx["recent_books"]) == ["RecentBook"]
    assert _titles(dash_ctx["upcoming_books"]) == ["UpcomingBook"]


def test_dashboard_month_filters_widen_window(auth_client, make_series, make_book, subscribe, today, dash_ctx):
    series = make_series("Filter Series")
    subscribe(auth_client.user, series)
    make_book(series, "OldBook", 1, today - datetime.timedelta(days=200))
    make_book(series, "FarFuture", 2, today + datetime.timedelta(days=200))
    auth_client.get("/")
    assert dash_ctx["recent_books"] == [] and dash_ctx["upcoming_books"] == []
    auth_client.get("/?recent_months=12&upcoming_months=12")
    assert _titles(dash_ctx["recent_books"]) == ["OldBook"]
    assert _titles(dash_ctx["upcoming_books"]) == ["FarFuture"]


def test_dashboard_month_filters_are_clamped(auth_client):
    assert auth_client.get("/?recent_months=0&upcoming_months=999").status_code == 200
    assert auth_client.get("/?recent_months=-5&upcoming_months=-5").status_code == 200


def test_dashboard_clamps_months_passed_to_template(auth_client, monkeypatch):
    captured = {}
    real = main.templates.TemplateResponse

    def spy(name, ctx, *a, **k):
        captured.update(ctx)
        return real(name, ctx, *a, **k)

    monkeypatch.setattr(main.templates, "TemplateResponse", spy)
    auth_client.get("/?recent_months=0&upcoming_months=999")
    assert captured["recent_months"] == 1
    assert captured["upcoming_months"] == 24


def test_dashboard_muted_series_excluded_from_sections(auth_client, make_series, make_book, subscribe, today, monkeypatch):
    series = make_series("Muted Series")
    subscribe(auth_client.user, series, muted=True)
    make_book(series, "MutedRecent", 1, today - datetime.timedelta(days=2))
    make_book(series, "MutedUpcoming", 2, today + datetime.timedelta(days=2))
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    assert auth_client.get("/").status_code == 200
    assert captured["recent_books"] == []
    assert captured["upcoming_books"] == []
    assert len(captured["rows"]) == 1 and captured["rows"][0]["muted"] is True


def test_dashboard_row_stats_and_sorting(auth_client, make_series, make_book, subscribe, session, today, monkeypatch):
    series = make_series("Stats Series")
    subscribe(auth_client.user, series)
    b1 = make_book(series, "B1", 1, today - datetime.timedelta(days=10), cover_image="c1.jpg")
    b2 = make_book(series, "B2", 2, today - datetime.timedelta(days=3))
    make_book(series, "B3", 3, today + datetime.timedelta(days=4))
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=b1.id, acknowledged=True))
    session.commit()
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.get("/")
    row = captured["rows"][0]
    assert row["released_count"] == 2
    assert row["watchlist_count"] == 1  # b1 acknowledged
    assert row["upcoming"].title == "B3"
    assert row["latest_released"].title == "B2"
    assert row["cover"] == "c1.jpg"
    assert [r["book"].title for r in captured["recent_books"]] == ["B2", "B1"]  # newest first
    assert captured["recent_books"][1]["acknowledged"] is True
    assert captured["abs_connected"] is False
    assert captured["prowlarr_connected"] is False
    assert "status" not in captured["recent_books"][0]


def test_dashboard_attaches_status_when_prowlarr_connected(auth_client, make_series, make_book, subscribe, session, today, monkeypatch):
    series = make_series("S")
    subscribe(auth_client.user, series)
    b = make_book(series, "B", 1, today - datetime.timedelta(days=1))
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=b.id, in_library=True))
    session.commit()
    set_prowlarr(session, auth_client.user)
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.get("/")
    assert captured["prowlarr_connected"] is True
    assert captured["recent_books"][0]["status"].in_library is True


def test_dashboard_abs_connected_flag(auth_client, session, monkeypatch):
    db_user = session.get(type(auth_client.user), auth_client.user.id)
    db_user.abs_base_url = "http://abs"
    db_user.abs_library_id = "lib"
    session.commit()
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.get("/")
    assert captured["abs_connected"] is True


def test_dashboard_impacted_row_shown_once(auth_client, make_series, subscribe, monkeypatch):
    series = make_series("Impacted")
    subscribe(auth_client.user, series)
    captured = []
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.append(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.post(f"/series/{series.id}/toggle-mute")
    auth_client.get("/")
    auth_client.get("/")
    assert captured[0]["impacted_row"]["series"].id == series.id
    assert captured[1]["impacted_row"] is None


# ---------------------------------------------------------------- search

def test_search_no_query(auth_client, monkeypatch):
    called = MagicMock()
    monkeypatch.setattr(main, "search_series", called)
    assert auth_client.get("/search").status_code == 200
    called.assert_not_called()


def test_search_results_and_subscribed_marking(auth_client, make_series, subscribe, monkeypatch):
    series = make_series("Known", asin="B0KNOWN001")
    subscribe(auth_client.user, series)
    results = [
        SeriesSearchResult("B0KNOWN001", "Known Series", "u1", "Auth", "s1"),
        SeriesSearchResult("B0OTHER001", "Other Series", "u2", None, "s2"),
    ]
    monkeypatch.setattr(main, "search_series", lambda q: results)
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    response = auth_client.get("/search?q=known")
    assert response.status_code == 200
    assert "Other Series" in response.text
    assert captured["subscribed_asins"] == {"B0KNOWN001"}
    assert captured["q"] == "known"
    assert captured["error"] is None


def test_search_failure_shows_error(auth_client, monkeypatch):
    def boom(q):
        raise RuntimeError("audible down")

    monkeypatch.setattr(main, "search_series", boom)
    response = auth_client.get("/search?q=x")
    assert response.status_code == 200
    assert "Search failed: audible down" in response.text


# ---------------------------------------------------------------- helpers

def test_set_impacted_series_writes_session():
    request = SimpleNamespace(session={})
    main._set_impacted_series(request, 42)
    assert request.session == {"impacted_series_id": 42}


def test_subscribe_to_url_creates_series_and_subscription(session, make_user, today, fake_fetch, monkeypatch):
    user = make_user()
    book = ScrapedBook("B0BOOK0001", "Book One", 1.0, today - datetime.timedelta(days=1), "u", None)
    fake_fetch["url"] = scraped(books=[book])
    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", lambda *a: 0)
    series_id = main._subscribe_to_url(session, user, "url")
    series = session.get(Series, series_id)
    assert series.asin == "B0NEWSER01"
    assert series.name == "Scraped Series"
    assert [b.title for b in series.books] == ["Book One"]
    assert session.query(Subscription).filter_by(user_id=user.id, series_id=series_id).count() == 1


def test_subscribe_to_url_existing_series_and_idempotent(session, make_user, make_series, fake_fetch):
    user = make_user()
    existing = make_series("Existing", asin="B0NEWSER01")
    fake_fetch["url"] = scraped()
    assert main._subscribe_to_url(session, user, "url") == existing.id
    assert main._subscribe_to_url(session, user, "url") == existing.id
    assert session.query(Series).count() == 1
    assert session.query(Subscription).count() == 1


def test_subscribe_to_url_propagates_series_page_error(session, make_user, fake_fetch):
    user = make_user()
    fake_fetch["bad"] = SeriesPageError("nope")
    with pytest.raises(SeriesPageError):
        main._subscribe_to_url(session, user, "bad")
    assert session.query(Series).count() == 0


def test_subscribe_to_url_swallows_reconcile_error(session, make_user, fake_fetch, monkeypatch):
    user = make_user()
    fake_fetch["url"] = scraped()

    def boom(*a):
        raise RuntimeError("abs broke")

    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", boom)
    series_id = main._subscribe_to_url(session, user, "url")
    assert session.query(Subscription).filter_by(series_id=series_id).count() == 1


# ---------------------------------------------------------------- /add

def test_add_form_renders(auth_client):
    assert auth_client.get("/add").status_code == 200


def test_add_series_success(auth_client, session, fake_fetch):
    fake_fetch["http://x"] = scraped()
    response = auth_client.post("/add", data={"url": "http://x"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    series = session.query(Series).one()
    assert session.query(Subscription).filter_by(user_id=auth_client.user.id, series_id=series.id).count() == 1


def test_add_series_page_error_renders_form_with_error(auth_client, session, fake_fetch):
    fake_fetch["http://bad"] = SeriesPageError("Not a series page")
    response = auth_client.post("/add", data={"url": "http://bad"})
    assert response.status_code == 200
    assert "Not a series page" in response.text
    assert session.query(Series).count() == 0


def test_add_series_already_subscribed_is_idempotent(auth_client, session, make_series, subscribe, fake_fetch):
    series = make_series("Dup", asin="B0NEWSER01")
    subscribe(auth_client.user, series)
    fake_fetch["http://x"] = scraped()
    response = auth_client.post("/add", data={"url": "http://x"})
    assert response.status_code == 303
    assert session.query(Subscription).count() == 1


def test_add_series_requires_url(auth_client):
    assert auth_client.post("/add", data={}).status_code == 422


# ---------------------------------------------------------------- /import

def test_import_form_renders(auth_client):
    assert auth_client.get("/import").status_code == 200


def test_import_preview_matches_and_flags_subscribed(auth_client, make_series, subscribe, no_sleep, monkeypatch):
    series = make_series("Sub", asin="B0SUBBED01")
    subscribe(auth_client.user, series)
    calls = []
    cands = {
        "first": [SeriesSearchResult("B0SUBBED01", "First", "u", None, "s")],
        "second": [SeriesSearchResult("B0FRESH001", "Second", "u", None, "s")],
        "third": [],
    }

    def fake_search(q):
        calls.append(q)
        return cands[q]

    monkeypatch.setattr(main, "search_series", fake_search)
    monkeypatch.setattr(main, "find_best_match", lambda q, c: c[0] if c else None)
    sleeps = []
    monkeypatch.setattr(main.time, "sleep", lambda s: sleeps.append(s))
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    response = auth_client.post("/import", data={"lines": "first\n\n  second  \nthird\n"})
    assert response.status_code == 200
    assert calls == ["first", "second", "third"]
    assert len(sleeps) == 2  # no delay before the first lookup
    r1, r2, r3 = captured["results"]
    assert r1["already_subscribed"] is True
    assert r2["already_subscribed"] is False
    assert r3["match"] is None and r3["already_subscribed"] is False
    assert r1["error"] is None


def test_import_preview_per_line_error_does_not_abort(auth_client, no_sleep, monkeypatch):
    def fake_search(q):
        if q == "bad":
            raise RuntimeError("503")
        return [SeriesSearchResult("B0GOOD0001", "Good", "u", None, "s")]

    monkeypatch.setattr(main, "search_series", fake_search)
    monkeypatch.setattr(main, "find_best_match", lambda q, c: c[0])
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.post("/import", data={"lines": "bad\ngood"})
    bad, good = captured["results"]
    assert bad["error"] == "503" and bad["match"] is None and bad["candidates"] == []
    assert good["error"] is None and good["match"].asin == "B0GOOD0001"


def test_import_preview_limits_to_60_lines_and_5_candidates(auth_client, no_sleep, monkeypatch):
    many = [SeriesSearchResult(f"B0ASIN{i:04d}", f"N{i}", "u", None, "s") for i in range(8)]
    monkeypatch.setattr(main, "search_series", lambda q: many)
    monkeypatch.setattr(main, "find_best_match", lambda q, c: None)
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.post("/import", data={"lines": "\n".join(f"q{i}" for i in range(70))})
    assert len(captured["results"]) == 60
    assert len(captured["results"][0]["candidates"]) == 5


def test_import_confirm_subscribes_all_and_skips_failures(auth_client, session, fake_fetch):
    fake_fetch["u1"] = scraped("B0SERIES001", "One")
    fake_fetch["u2"] = SeriesPageError("bad")
    fake_fetch["u3"] = scraped("B0SERIES003", "Three")
    response = auth_client.post("/import/confirm", data={"urls": ["u1", "u2", "u3"]})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert {s.asin for s in session.query(Series).all()} == {"B0SERIES001", "B0SERIES003"}
    assert session.query(Subscription).count() == 2


def test_import_confirm_no_urls(auth_client, session):
    response = auth_client.post("/import/confirm", data={})
    assert response.status_code == 303
    assert session.query(Subscription).count() == 0


def test_import_confirm_sets_impacted_to_last_added(auth_client, session, fake_fetch, monkeypatch):
    fake_fetch["u1"] = scraped("B0SERIES001", "One")
    fake_fetch["u2"] = scraped("B0SERIES002", "Two")
    auth_client.post("/import/confirm", data={"urls": ["u1", "u2"]})
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.get("/")
    assert captured["impacted_row"]["series"].asin == "B0SERIES002"


# ---------------------------------------------------------------- _require_subscription / _redirect

def test_require_subscription(session, make_user, make_series, subscribe):
    user, other = make_user("a"), make_user("b")
    series = make_series()
    subscribe(user, series)
    assert main._require_subscription(session, user, series.id) is not None
    assert main._require_subscription(session, other, series.id) is None
    assert main._require_subscription(session, user, 9999) is None


@pytest.mark.parametrize(
    "referer,expected",
    [
        ("http://testserver/series/7", "/series/7"),
        ("http://testserver/series/7/", "/series/7"),
        ("http://testserver/series/8", "/"),
        ("http://testserver/", "/"),
        ("", "/"),
    ],
)
def test_redirect_series_or_dash(referer, expected):
    headers = {"referer": referer} if referer else {}
    request = SimpleNamespace(headers=headers)
    response = main._redirect_series_or_dash(request, 7)
    assert response.status_code == 303
    assert response.headers["location"] == expected


# ---------------------------------------------------------------- refresh

def test_refresh_subscribed_calls_refresh_series(auth_client, make_series, subscribe, monkeypatch):
    series = make_series()
    subscribe(auth_client.user, series)
    refresh = MagicMock()
    monkeypatch.setattr(main, "refresh_series", refresh)
    response = auth_client.post(f"/series/{series.id}/refresh")
    assert response.status_code == 303 and response.headers["location"] == "/"
    refresh.assert_called_once_with(series.id)


def test_refresh_redirects_back_to_series_page(auth_client, make_series, subscribe, monkeypatch):
    series = make_series()
    subscribe(auth_client.user, series)
    monkeypatch.setattr(main, "refresh_series", MagicMock())
    response = auth_client.post(
        f"/series/{series.id}/refresh", headers={"referer": f"http://testserver/series/{series.id}"}
    )
    assert response.headers["location"] == f"/series/{series.id}"


def test_refresh_not_subscribed_is_noop(auth_client, make_series, monkeypatch):
    series = make_series()
    refresh = MagicMock()
    monkeypatch.setattr(main, "refresh_series", refresh)
    response = auth_client.post(f"/series/{series.id}/refresh")
    assert response.status_code == 303
    refresh.assert_not_called()


# ---------------------------------------------------------------- toggle-ended / toggle-mute

def test_toggle_ended_flips_flag(auth_client, session, make_series, subscribe):
    series = make_series()
    subscribe(auth_client.user, series)
    assert auth_client.post(f"/series/{series.id}/toggle-ended").status_code == 303
    session.expire_all()
    assert session.get(Series, series.id).ended is True
    auth_client.post(f"/series/{series.id}/toggle-ended")
    session.expire_all()
    assert session.get(Series, series.id).ended is False


def test_toggle_ended_not_subscribed_denied(auth_client, session, make_series):
    series = make_series()
    response = auth_client.post(f"/series/{series.id}/toggle-ended")
    assert response.status_code == 303
    session.expire_all()
    assert session.get(Series, series.id).ended is False


def test_toggle_mute_flips_subscription(auth_client, session, make_series, subscribe):
    series = make_series()
    subscribe(auth_client.user, series)
    auth_client.post(f"/series/{series.id}/toggle-mute")
    session.expire_all()
    assert session.query(Subscription).one().muted is True
    auth_client.post(f"/series/{series.id}/toggle-mute")
    session.expire_all()
    assert session.query(Subscription).one().muted is False


def test_toggle_mute_only_affects_own_subscription(auth_client, session, make_user, make_series, subscribe):
    series = make_series()
    subscribe(auth_client.user, series)
    other_sub = subscribe(make_user("bob"), series)
    auth_client.post(f"/series/{series.id}/toggle-mute")
    session.expire_all()
    assert session.get(Subscription, other_sub.id).muted is False


def test_toggle_mute_not_subscribed_is_noop(auth_client, session, make_user, make_series, subscribe):
    series = make_series()
    other_sub = subscribe(make_user("bob"), series)
    response = auth_client.post(f"/series/{series.id}/toggle-mute")
    assert response.status_code == 303
    session.expire_all()
    assert session.get(Subscription, other_sub.id).muted is False


# ---------------------------------------------------------------- unsubscribe

def test_unsubscribe_last_subscriber_removes_series(auth_client, session, make_series, subscribe):
    series = make_series()
    sid = series.id
    subscribe(auth_client.user, series)
    response = auth_client.post(f"/series/{sid}/unsubscribe")
    assert response.status_code == 303 and response.headers["location"] == "/"
    session.expire_all()
    assert session.get(Series, sid) is None
    assert session.query(Subscription).count() == 0


def test_unsubscribe_keeps_series_for_other_subscribers(auth_client, session, make_user, make_series, subscribe):
    series = make_series()
    subscribe(auth_client.user, series)
    subscribe(make_user("bob"), series)
    auth_client.post(f"/series/{series.id}/unsubscribe")
    session.expire_all()
    assert session.get(Series, series.id) is not None
    assert sub_count(session, series.id) == 1


def test_unsubscribe_not_subscribed_does_not_touch_others(auth_client, session, make_user, make_series, subscribe):
    series = make_series()
    subscribe(make_user("bob"), series)
    response = auth_client.post(f"/series/{series.id}/unsubscribe")
    assert response.status_code == 303
    session.expire_all()
    assert session.get(Series, series.id) is not None
    assert sub_count(session, series.id) == 1


def test_unsubscribe_clears_impacted_series(auth_client, make_series, subscribe, monkeypatch):
    series = make_series()
    subscribe(auth_client.user, series)
    auth_client.post(f"/series/{series.id}/toggle-mute")  # sets impacted
    auth_client.post(f"/series/{series.id}/unsubscribe")
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    auth_client.get("/")
    assert captured["impacted_row"] is None


# ---------------------------------------------------------------- series detail

def test_series_detail_subscribed(auth_client, make_series, make_book, subscribe, session, today, monkeypatch):
    series = make_series("Detail Series")
    subscribe(auth_client.user, series)
    b1 = make_book(series, "First Book", 1, today - datetime.timedelta(days=30))
    make_book(series, "Second Book", 2, today + datetime.timedelta(days=30))
    make_book(series, "Unnumbered", None, None)
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=b1.id, in_library=True))
    session.commit()
    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", lambda *a: 0)
    captured = {}
    real = main.templates.TemplateResponse
    monkeypatch.setattr(
        main.templates, "TemplateResponse",
        lambda n, ctx, *a, **k: (captured.update(ctx), real(n, ctx, *a, **k))[1],
    )
    response = auth_client.get(f"/series/{series.id}")
    assert response.status_code == 200
    assert "Detail Series" in response.text
    assert [b.title for b in captured["books"]] == ["First Book", "Second Book", "Unnumbered"]
    assert captured["released_count"] == 1
    assert captured["watchlist_count"] == 1
    assert captured["in_library_unack_count"] == 1
    assert captured["book_entries"][0]["status"].in_library is True
    assert captured["book_entries"][1]["status"] is None
    assert captured["book_entries"][2]["relative"] == ""
    assert captured["book_entries"][0]["relative"] != ""


def test_series_detail_reconcile_failure_still_renders(auth_client, make_series, subscribe, monkeypatch):
    series = make_series()
    subscribe(auth_client.user, series)

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", boom)
    assert auth_client.get(f"/series/{series.id}").status_code == 200


def test_series_detail_not_subscribed_404(auth_client, make_user, make_series, subscribe):
    series = make_series()
    subscribe(make_user("bob"), series)
    assert auth_client.get(f"/series/{series.id}").status_code == 404


def test_series_detail_missing_404(auth_client):
    assert auth_client.get("/series/9999").status_code == 404


# ---------------------------------------------------------------- acknowledge-in-library

def test_acknowledge_in_library_marks_released_in_library_books(
    auth_client, session, make_series, make_book, subscribe, today, monkeypatch
):
    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", lambda *a: 0)
    series = make_series()
    subscribe(auth_client.user, series)
    released = make_book(series, "R", 1, today - datetime.timedelta(days=1))
    not_in_lib = make_book(series, "N", 2, today - datetime.timedelta(days=1))
    future = make_book(series, "F", 3, today + datetime.timedelta(days=9))
    uid = auth_client.user.id
    session.add_all([
        UserBookStatus(user_id=uid, book_id=released.id, in_library=True),
        UserBookStatus(user_id=uid, book_id=not_in_lib.id, in_library=False),
        UserBookStatus(user_id=uid, book_id=future.id, in_library=True),
    ])
    session.commit()
    response = auth_client.post(f"/series/{series.id}/acknowledge-in-library")
    assert response.status_code == 303
    assert response.headers["location"] == f"/series/{series.id}"
    session.expire_all()
    st = {s.book_id: s for s in session.query(UserBookStatus).all()}
    assert st[released.id].acknowledged is True and st[released.id].acknowledged_at is not None
    assert st[not_in_lib.id].acknowledged is False
    assert st[future.id].acknowledged is False


def test_acknowledge_in_library_tolerates_reconcile_error(auth_client, make_series, subscribe, monkeypatch):
    series = make_series()
    subscribe(auth_client.user, series)

    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(main, "reconcile_series_with_cached_asins", boom)
    assert auth_client.post(f"/series/{series.id}/acknowledge-in-library").status_code == 303


def test_acknowledge_in_library_not_subscribed_404_and_untouched(
    auth_client, session, make_user, make_series, make_book, subscribe, today
):
    bob = make_user("bob")
    series = make_series()
    subscribe(bob, series)
    book = make_book(series, "R", 1, today - datetime.timedelta(days=1))
    session.add(UserBookStatus(user_id=bob.id, book_id=book.id, in_library=True))
    session.commit()
    assert auth_client.post(f"/series/{series.id}/acknowledge-in-library").status_code == 404
    session.expire_all()
    assert session.query(UserBookStatus).one().acknowledged is False


# ---------------------------------------------------------------- _require_subscription_for_book

def test_require_subscription_for_book(session, make_user, make_series, make_book, subscribe):
    user, other = make_user("a"), make_user("b")
    series = make_series()
    subscribe(user, series)
    book = make_book(series)
    assert main._require_subscription_for_book(session, user, book.id).id == book.id
    assert main._require_subscription_for_book(session, other, book.id) is None
    assert main._require_subscription_for_book(session, user, 9999) is None


# ---------------------------------------------------------------- download form

def result(**kw):
    defaults = dict(guid="g1", indexer_id=1, indexer_name="Idx", title="Some Release",
                    size=1000, seeders=5, protocol="torrent", publish_date=None)
    defaults.update(kw)
    return ProwlarrResult(**defaults)


@pytest.fixture
def prowlarr(monkeypatch):
    client = MagicMock()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(main, "ProwlarrClient", factory)
    client.factory = factory
    return client


@pytest.fixture
def owned_book(auth_client, make_series, make_book, subscribe):
    series = make_series()
    subscribe(auth_client.user, series)
    return make_book(series, "Downloadable Title", 1)


def test_download_form_lists_results(auth_client, owned_book, session, prowlarr):
    set_prowlarr(session, auth_client.user, "http://p.local", "secret")
    prowlarr.search.return_value = [result(title="Great Release File")]
    response = auth_client.get(f"/books/{owned_book.id}/download")
    assert response.status_code == 200
    assert "Great Release File" in response.text
    prowlarr.factory.assert_called_once_with("http://p.local", "secret")
    prowlarr.search.assert_called_once_with("Downloadable Title")


def test_download_form_prowlarr_error_shown(auth_client, owned_book, session, prowlarr):
    set_prowlarr(session, auth_client.user)
    prowlarr.search.side_effect = ProwlarrError("indexer exploded")
    response = auth_client.get(f"/books/{owned_book.id}/download")
    assert response.status_code == 200
    assert "indexer exploded" in response.text


def test_download_form_without_prowlarr_redirects_to_integrations(auth_client, owned_book, prowlarr):
    response = auth_client.get(f"/books/{owned_book.id}/download")
    assert response.status_code == 303
    assert response.headers["location"] == "/account/integrations"
    prowlarr.search.assert_not_called()


def test_download_form_unsubscribed_or_missing_book_redirects_home(
    auth_client, session, make_user, make_series, make_book, subscribe, prowlarr
):
    set_prowlarr(session, auth_client.user)
    series = make_series()
    subscribe(make_user("bob"), series)
    book = make_book(series)
    for book_id in (book.id, 9999):
        response = auth_client.get(f"/books/{book_id}/download")
        assert response.status_code == 303 and response.headers["location"] == "/"
    prowlarr.search.assert_not_called()


# ---------------------------------------------------------------- grab

def test_grab_success_records_request(auth_client, owned_book, session, prowlarr):
    set_prowlarr(session, auth_client.user)
    response = auth_client.post(
        f"/books/{owned_book.id}/download/grab", data={"guid": "g1", "indexer_id": "7"}
    )
    assert response.status_code == 303 and response.headers["location"] == "/"
    prowlarr.grab.assert_called_once_with("g1", 7)
    session.expire_all()
    status = session.query(UserBookStatus).one()
    assert status.user_id == auth_client.user.id
    assert status.requested_at is not None
    assert status.last_error is None


def test_grab_success_clears_previous_error_and_reuses_row(auth_client, owned_book, session, prowlarr):
    set_prowlarr(session, auth_client.user)
    session.add(UserBookStatus(user_id=auth_client.user.id, book_id=owned_book.id, last_error="old"))
    session.commit()
    auth_client.post(f"/books/{owned_book.id}/download/grab", data={"guid": "g", "indexer_id": "1"})
    session.expire_all()
    status = session.query(UserBookStatus).one()
    assert status.last_error is None and status.requested_at is not None


def test_grab_prowlarr_error_saved_and_truncated(auth_client, owned_book, session, prowlarr):
    set_prowlarr(session, auth_client.user)
    prowlarr.grab.side_effect = ProwlarrError("x" * 800)
    response = auth_client.post(
        f"/books/{owned_book.id}/download/grab", data={"guid": "g", "indexer_id": "1"}
    )
    assert response.status_code == 303
    session.expire_all()
    status = session.query(UserBookStatus).one()
    assert status.requested_at is None
    assert status.last_error == "x" * 500


def test_grab_without_prowlarr_redirects_to_integrations(auth_client, owned_book, session, prowlarr):
    response = auth_client.post(
        f"/books/{owned_book.id}/download/grab", data={"guid": "g", "indexer_id": "1"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/account/integrations"
    prowlarr.grab.assert_not_called()
    assert session.query(UserBookStatus).count() == 0


def test_grab_unsubscribed_book_denied(auth_client, session, make_user, make_series, make_book, subscribe, prowlarr):
    set_prowlarr(session, auth_client.user)
    series = make_series()
    subscribe(make_user("bob"), series)
    book = make_book(series)
    response = auth_client.post(f"/books/{book.id}/download/grab", data={"guid": "g", "indexer_id": "1"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    prowlarr.grab.assert_not_called()
    assert session.query(UserBookStatus).count() == 0


def test_grab_validates_form(auth_client, owned_book):
    assert auth_client.post(f"/books/{owned_book.id}/download/grab", data={"guid": "g"}).status_code == 422
    r = auth_client.post(f"/books/{owned_book.id}/download/grab", data={"guid": "g", "indexer_id": "abc"})
    assert r.status_code == 422
