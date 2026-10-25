import datetime
import json
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.exc import IntegrityError

from app import scheduler as sched
from app.audiobookshelf import ABSError
from app.models import Book, PushSubscription, Series, UserBookStatus
from app.scraper import ScrapedBook, ScrapedSeries, SeriesPageError

DAY = datetime.timedelta(days=1)


def sb(asin, title=None, position=1.0, release_date=None, image_url=None):
    return ScrapedBook(
        asin=asin,
        title=title or f"Title {asin}",
        position=position,
        release_date=release_date,
        url=f"https://x/{asin}",
        image_url=image_url,
    )


def scraped(books, name="Scraped Name"):
    return ScrapedSeries(asin="S", name=name, url="https://x/s", books=books)


@pytest.fixture
def push(monkeypatch):
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(sched, "send_push", mock)
    return mock


@pytest.fixture(autouse=True)
def _reset_scan_state():
    sched._scanning_users.clear()
    sched._scan_progress.clear()
    yield
    sched._scanning_users.clear()
    sched._scan_progress.clear()


@pytest.fixture
def add_push(session):
    counter = {"n": 0}

    def _add(user, n=1):
        subs = []
        for _ in range(n):
            counter["n"] += 1
            p = PushSubscription(user_id=user.id, endpoint=f"https://push/{user.id}/{counter['n']}", p256dh="k", auth="a")
            session.add(p)
            subs.append(p)
        session.commit()
        return subs

    return _add


@pytest.fixture
def abs_user(make_user):
    return make_user("absu", abs_base_url="http://abs", abs_api_key="key", abs_library_id="lib")


# ---------- _pick_icon ----------

def test_pick_icon_returns_first_cover():
    books = [Book(cover_image=None), Book(cover_image="a.jpg"), Book(cover_image="b.jpg")]
    assert sched._pick_icon(books) == "a.jpg"


def test_pick_icon_none_when_no_covers_or_empty():
    assert sched._pick_icon([Book(cover_image=None), Book(cover_image="")]) is None
    assert sched._pick_icon([]) is None


# ---------- _push_to_series_subscribers ----------

def test_push_to_subscribers_sends_to_unmuted_only(session, make_user, make_series, subscribe, add_push, push):
    series = make_series("S1")
    u1, u2 = make_user("u1"), make_user("u2")
    subscribe(u1, series)
    subscribe(u2, series, muted=True)
    add_push(u1, 2)
    add_push(u2)
    sched._push_to_series_subscribers(session, series, "hello", "icon.jpg")
    assert push.call_count == 2
    for call in push.call_args_list:
        assert call.kwargs == {"title": "S1", "body": "hello", "url": "/", "icon": "icon.jpg"}
        assert call.args[0].user_id == u1.id


def test_push_to_subscribers_no_subscribers_no_calls(session, make_series, push):
    sched._push_to_series_subscribers(session, make_series(), "x", None)
    push.assert_not_called()


def test_push_to_subscribers_deletes_dead_subscriptions(session, make_user, make_series, subscribe, add_push, push):
    series = make_series()
    user = make_user()
    subscribe(user, series)
    add_push(user, 2)
    push.side_effect = [True, False]
    sched._push_to_series_subscribers(session, series, "x", None)
    assert session.query(PushSubscription).count() == 1


# ---------- _notify_new_and_dated ----------

def _bodies(push):
    return [c.kwargs["body"] for c in push.call_args_list]


@pytest.fixture
def notif_setup(session, make_user, make_series, subscribe, add_push):
    series = make_series("Ser")
    user = make_user()
    subscribe(user, series)
    add_push(user)
    return session, series


def test_notify_new_single(notif_setup, push):
    session, series = notif_setup
    sched._notify_new_and_dated(session, series, [Book(title="Foo", cover_image="c.jpg")], [])
    assert _bodies(push) == ["New book: Foo"]
    assert push.call_args.kwargs["icon"] == "c.jpg"


def test_notify_new_multiple(notif_setup, push):
    session, series = notif_setup
    sched._notify_new_and_dated(session, series, [Book(title="a"), Book(title="b")], [])
    assert _bodies(push) == ["2 new books added"]


def test_notify_dated_single_and_multiple(notif_setup, push):
    session, series = notif_setup
    sched._notify_new_and_dated(session, series, [], [Book(title="Foo")])
    sched._notify_new_and_dated(session, series, [], [Book(title="a"), Book(title="b"), Book(title="c")])
    assert _bodies(push) == ["Release date announced for Foo", "Release dates announced for 3 books"]


def test_notify_new_and_dated_combined(notif_setup, push):
    session, series = notif_setup
    sched._notify_new_and_dated(session, series, [Book(title="N")], [Book(title="D")])
    assert _bodies(push) == ["New book: N · Release date announced for D"]


# ---------- _notify_released_today ----------

def test_notify_released_today_single_marks_notified(notif_setup, push):
    session, series = notif_setup
    book = Book(series_id=series.id, asin="A1", title="Big", url="u", cover_image="c.png")
    session.add(book)
    session.commit()
    sched._notify_released_today(session, series, [book])
    assert _bodies(push) == ["🎉 Big is out today!"]
    assert push.call_args.kwargs["icon"] == "c.png"
    session.refresh(book)
    assert book.release_day_notified is True


def test_notify_released_today_multiple(notif_setup, push):
    session, series = notif_setup
    books = [Book(series_id=series.id, asin=f"A{i}", title=f"T{i}", url="u") for i in range(2)]
    session.add_all(books)
    session.commit()
    sched._notify_released_today(session, series, books)
    assert _bodies(push) == ["🎉 2 books are out today!"]
    assert all(b.release_day_notified for b in books)


# ---------- update_series_from_scraped ----------

@pytest.fixture
def subscribed(session, make_user, make_series, subscribe, add_push):
    series = make_series("Old Name")
    user = make_user()
    subscribe(user, series)
    add_push(user)
    return series


def test_first_scrape_adds_books_without_notifying(session, subscribed, today, push):
    series = subscribed
    series.consecutive_failures = 3
    series.last_failure_reason = "boom"
    series.last_failure_at = datetime.datetime.utcnow()
    session.commit()
    data = scraped([
        sb("A1", "One", 1.0, today - DAY, "img1"),
        sb("A2", "Two", 2.0, today),
        sb("A3", "Three", 3.0, today + DAY),
        sb("A4", "Four", 4.0, None),
    ])
    sched.update_series_from_scraped(session, series, data)
    push.assert_not_called()
    session.refresh(series)
    assert series.name == "Scraped Name"
    assert series.last_checked is not None
    assert series.consecutive_failures == 0
    assert series.last_failure_at is None and series.last_failure_reason is None
    books = {b.asin: b for b in series.books}
    assert len(books) == 4
    assert books["A1"].title == "One" and books["A1"].cover_image == "img1"
    assert books["A1"].url == "https://x/A1" and books["A1"].position == 1.0
    assert books["A1"].release_day_notified is True
    assert books["A2"].release_day_notified is True
    assert not books["A3"].release_day_notified
    assert not books["A4"].release_day_notified


def test_later_scrape_new_book_notifies(session, subscribed, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    session.commit()
    sched.update_series_from_scraped(session, series, scraped([sb("N1", "Fresh", 1.0, today + DAY)]))
    assert _bodies(push) == ["New book: Fresh"]


def test_later_scrape_multiple_new_books(session, subscribed, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    session.commit()
    sched.update_series_from_scraped(session, series, scraped([sb("N1"), sb("N2", release_date=today + DAY)]))
    assert _bodies(push) == ["2 new books added"]


def test_later_scrape_new_book_releasing_today_is_released_notification(session, subscribed, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    session.commit()
    sched.update_series_from_scraped(session, series, scraped([sb("N1", "Today", 1.0, today)]))
    assert _bodies(push) == ["🎉 Today is out today!"]
    book = session.query(Book).filter_by(asin="N1").one()
    assert book.release_day_notified is True


def test_date_announced_for_existing_book(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    book = make_book(series, asin="E1", title="Old", release_date=None)
    session.commit()
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "Old", 1.0, today + 5 * DAY)]))
    assert _bodies(push) == ["Release date announced for Old"]
    session.refresh(book)
    assert book.release_date == today + 5 * DAY


def test_date_announced_as_today_is_release_notification(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    make_book(series, asin="E1", title="Old", release_date=None)
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "Old", 1.0, today)]))
    assert _bodies(push) == ["🎉 Old is out today!"]


def test_existing_book_released_today_not_yet_notified(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    make_book(series, asin="E1", title="Old", release_date=today)
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "Old", 1.0, today)]))
    assert _bodies(push) == ["🎉 Old is out today!"]
    # A second refresh must not notify again
    push.reset_mock()
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "Old", 1.0, today)]))
    push.assert_not_called()


def test_no_change_no_notification_and_dedupes_existing(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    make_book(series, asin="E1", title="Old", release_date=today - DAY)
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "Renamed", 2.0, today - DAY)]))
    push.assert_not_called()
    session.refresh(series)
    assert len(series.books) == 1
    assert series.books[0].title == "Renamed" and series.books[0].position == 2.0


def test_changed_date_between_two_dates_not_announced(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    book = make_book(series, asin="E1", release_date=today + DAY)
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "T", 1.0, today + 3 * DAY)]))
    push.assert_not_called()
    session.refresh(book)
    assert book.release_date == today + 3 * DAY


def test_first_scrape_existing_book_with_no_date_gets_no_notification(session, subscribed, make_book, today, push):
    series = subscribed
    make_book(series, asin="E1", release_date=None)
    sched.update_series_from_scraped(session, series, scraped([sb("E1", "T", 1.0, today + DAY)]))
    push.assert_not_called()


def test_new_and_released_both_notify(session, subscribed, make_book, today, push):
    series = subscribed
    series.last_checked = datetime.datetime.utcnow()
    make_book(series, asin="E1", title="Old", release_date=None)
    sched.update_series_from_scraped(
        session, series, scraped([sb("E1", "Old", 1.0, today), sb("N1", "New", 2.0, today + DAY)])
    )
    assert sorted(_bodies(push)) == sorted(["New book: New", "🎉 Old is out today!"])


# ---------- refresh_series ----------

@pytest.fixture
def no_sleep(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(sched.time, "sleep", m)
    return m


def test_refresh_series_missing_id_is_noop(monkeypatch, no_sleep):
    fetch = MagicMock()
    monkeypatch.setattr(sched, "fetch_series", fetch)
    sched.refresh_series(9999)
    fetch.assert_not_called()


def test_refresh_series_success_updates(session, make_series, monkeypatch, no_sleep, push):
    series = make_series("Old")
    fetch = MagicMock(return_value=scraped([sb("A1", "One")], name="New Name"))
    monkeypatch.setattr(sched, "fetch_series", fetch)
    sched.refresh_series(series.id)
    fetch.assert_called_once_with(series.url)
    no_sleep.assert_not_called()
    session.expire_all()
    s = session.get(Series, series.id)
    assert s.name == "New Name" and len(s.books) == 1 and s.consecutive_failures == 0


def test_refresh_series_retries_then_succeeds(session, make_series, monkeypatch, no_sleep, push):
    series = make_series("Old")
    fetch = MagicMock(side_effect=[SeriesPageError("waf"), ValueError("odd"), scraped([sb("A1")], name="Ok")])
    monkeypatch.setattr(sched, "fetch_series", fetch)
    sched.refresh_series(series.id)
    assert fetch.call_count == 3
    assert no_sleep.call_count == 2
    no_sleep.assert_called_with(3.0)
    session.expire_all()
    s = session.get(Series, series.id)
    assert s.name == "Ok" and s.consecutive_failures == 0


def test_refresh_series_all_attempts_fail_records_failure(session, make_series, monkeypatch, no_sleep):
    series = make_series("Old")
    fetch = MagicMock(side_effect=SeriesPageError("nothing " + "x" * 600))
    monkeypatch.setattr(sched, "fetch_series", fetch)
    sched.refresh_series(series.id)
    sched.refresh_series(series.id)
    assert fetch.call_count == 6
    session.expire_all()
    s = session.get(Series, series.id)
    assert s.consecutive_failures == 2
    assert s.last_failure_at is not None
    assert s.last_failure_reason.startswith("nothing") and len(s.last_failure_reason) == 500
    assert s.name == "Old" and s.last_checked is None


def test_refresh_series_unexpected_exception_recorded(session, make_series, monkeypatch, no_sleep):
    series = make_series()
    monkeypatch.setattr(sched, "fetch_series", MagicMock(side_effect=RuntimeError("kaboom")))
    sched.refresh_series(series.id)
    session.expire_all()
    s = session.get(Series, series.id)
    assert s.consecutive_failures == 1 and s.last_failure_reason == "kaboom"


# ---------- refresh_all_series ----------

def test_refresh_all_series_sleeps_between_and_continues_after_error(make_series, monkeypatch, no_sleep):
    s1, s2, s3 = make_series("a"), make_series("b"), make_series("c")
    calls = []

    def fake(series_id):
        calls.append(series_id)
        if series_id == s2.id:
            raise RuntimeError("bad")

    monkeypatch.setattr(sched, "refresh_series", fake)
    sched.refresh_all_series()
    assert sorted(calls) == sorted([s1.id, s2.id, s3.id])
    assert no_sleep.call_count == 2
    no_sleep.assert_called_with(1.0)


def test_refresh_all_series_empty(monkeypatch, no_sleep):
    fake = MagicMock()
    monkeypatch.setattr(sched, "refresh_series", fake)
    sched.refresh_all_series()
    fake.assert_not_called()
    no_sleep.assert_not_called()


# ---------- scan progress helpers ----------

def test_mark_user_scanning_sets_state_once():
    assert sched.mark_user_scanning(1) is True
    assert sched.is_user_scanning(1) is True
    assert sched.mark_user_scanning(1) is False
    progress = sched.get_user_scan_progress(1)
    assert progress["scanning"] is True and progress["phase"] == "starting" and progress["percent"] == 5


def test_is_user_scanning_false_by_default():
    assert sched.is_user_scanning(42) is False


def test_get_user_scan_progress_none_when_unknown():
    assert sched.get_user_scan_progress(42) is None


def test_get_user_scan_progress_returns_copy():
    sched.mark_user_scanning(1)
    p = sched.get_user_scan_progress(1)
    p["percent"] = 99
    assert sched.get_user_scan_progress(1)["percent"] == 5


def test_get_user_scan_progress_fallback_when_scanning_without_progress():
    sched._scanning_users.add(7)
    assert sched.get_user_scan_progress(7) == {
        "scanning": True, "phase": "starting", "message": "Starting scan...", "percent": 0,
    }


# ---------- run_scan_for_user ----------

def _patch_abs(monkeypatch, asins=None, exc=None, pages=None):
    instance = MagicMock()

    def list_asins(library_id, progress_cb=None):
        if pages:
            for args in pages:
                progress_cb(*args)
        if exc:
            raise exc
        return asins

    instance.list_asins_in_library.side_effect = list_asins
    cls = MagicMock(return_value=instance)
    monkeypatch.setattr(sched, "ABSClient", cls)
    return cls, instance


def test_run_scan_unknown_user_reports_not_configured():
    sched._scanning_users.add(999)
    sched.run_scan_for_user(999)
    p = sched.get_user_scan_progress(999)
    assert p["phase"] == "error" and p["error"] == "Not configured" and p["scanning"] is False
    assert not sched.is_user_scanning(999)


def test_run_scan_unconfigured_user(make_user):
    user = make_user("plain", abs_base_url="http://abs")
    sched.run_scan_for_user(user.id)
    assert sched.get_user_scan_progress(user.id)["error"] == "Not configured"


def test_run_scan_flags_books_and_writes_cache(session, abs_user, make_series, make_book, subscribe, today, monkeypatch):
    series = make_series()
    subscribe(abs_user, series)
    b_in = make_book(series, asin="b0in", release_date=today - DAY)
    b_out = make_book(series, asin="B0OUT", release_date=today - DAY)
    b_future = make_book(series, asin="B0FUT", release_date=today + DAY)
    b_nodate = make_book(series, asin="B0ND", release_date=None)
    other_series = make_series("unsubscribed")
    b_other = make_book(other_series, asin="B0OTH", release_date=today - DAY)
    cls, _ = _patch_abs(monkeypatch, asins={"B0IN", "B0OTH", "B0FUT"}, pages=[(1, 2, 10, 20), (2, None, 20, None)])
    sched.mark_user_scanning(abs_user.id)
    sched.run_scan_for_user(abs_user.id)

    cls.assert_called_once_with("http://abs", "key")
    session.expire_all()
    st = {s.book_id: s.in_library for s in session.query(UserBookStatus).all()}
    assert st == {b_in.id: True, b_out.id: False}
    assert b_future.id not in st and b_nodate.id not in st and b_other.id not in st
    p = sched.get_user_scan_progress(abs_user.id)
    assert p["phase"] == "complete" and p["percent"] == 100 and p["scanning"] is False
    assert "2 books evaluated (1 in library)" in p["message"]
    assert p["last_scanned_at"].endswith("UTC")
    assert not sched.is_user_scanning(abs_user.id)
    cache = sched.DATA_DIR / f"abs_asins_{abs_user.id}.json"
    assert set(json.loads(cache.read_text())) == {"B0IN", "B0OTH", "B0FUT"}


def test_run_scan_keeps_already_in_library_and_updates_existing_status(
    session, abs_user, make_series, make_book, subscribe, today, monkeypatch
):
    series = make_series()
    subscribe(abs_user, series)
    kept = make_book(series, asin="B0KEPT", release_date=today - DAY)
    flip = make_book(series, asin="B0FLIP", release_date=today - DAY)
    old = datetime.datetime(2020, 1, 1)
    session.add_all([
        UserBookStatus(user_id=abs_user.id, book_id=kept.id, in_library=True, checked_at=old),
        UserBookStatus(user_id=abs_user.id, book_id=flip.id, in_library=False, checked_at=old),
    ])
    session.commit()
    _patch_abs(monkeypatch, asins={"B0FLIP"})  # kept is absent from ABS now but stays True
    sched.run_scan_for_user(abs_user.id)
    session.expire_all()
    st = {s.book_id: s for s in session.query(UserBookStatus).all()}
    assert st[kept.id].in_library is True and st[kept.id].checked_at > old
    assert st[flip.id].in_library is True and st[flip.id].checked_at > old
    assert "2 in library" in sched.get_user_scan_progress(abs_user.id)["message"]


def test_run_scan_progress_callback_values(abs_user, monkeypatch):
    seen = []
    instance = MagicMock()

    def list_asins(library_id, progress_cb=None):
        progress_cb(1, 4, 5, 20)
        seen.append(sched.get_user_scan_progress(abs_user.id))
        progress_cb(2, None, 9, None)
        seen.append(sched.get_user_scan_progress(abs_user.id))
        progress_cb(50, None, 9, None)
        seen.append(sched.get_user_scan_progress(abs_user.id))
        progress_cb(10, 10, 9, 9)
        seen.append(sched.get_user_scan_progress(abs_user.id))
        return set()

    instance.list_asins_in_library.side_effect = list_asins
    monkeypatch.setattr(sched, "ABSClient", MagicMock(return_value=instance))
    sched.run_scan_for_user(abs_user.id)
    assert seen[0]["percent"] == int(1 / 4 * 90) and "page 1 of 4" in seen[0]["message"]
    assert seen[0]["items_fetched"] == 5 and seen[0]["total_items"] == 20 and seen[0]["total_pages"] == 4
    assert seen[1]["percent"] == 40 and "page 2 (9 ASINs" in seen[1]["message"]
    assert seen[2]["percent"] == 90
    assert seen[3]["percent"] == 90


def test_run_scan_abs_error(abs_user, monkeypatch):
    _patch_abs(monkeypatch, exc=ABSError("nope"))
    sched.mark_user_scanning(abs_user.id)
    sched.run_scan_for_user(abs_user.id)
    p = sched.get_user_scan_progress(abs_user.id)
    assert p["phase"] == "error" and p["error"] == "nope" and p["message"] == "Scan failed: nope"
    assert not sched.is_user_scanning(abs_user.id)


def test_run_scan_cache_write_failure_is_ignored(abs_user, monkeypatch):
    _patch_abs(monkeypatch, asins={"X"})

    class BadDir:
        def __truediv__(self, other):
            raise OSError("readonly")

    monkeypatch.setattr(sched, "DATA_DIR", BadDir())
    sched.run_scan_for_user(abs_user.id)
    assert sched.get_user_scan_progress(abs_user.id)["phase"] == "complete"


def test_run_scan_unexpected_exception(abs_user, monkeypatch):
    instance = MagicMock()
    instance.list_asins_in_library.side_effect = RuntimeError("weird")
    monkeypatch.setattr(sched, "ABSClient", MagicMock(return_value=instance))
    sched.mark_user_scanning(abs_user.id)
    sched.run_scan_for_user(abs_user.id)
    p = sched.get_user_scan_progress(abs_user.id)
    assert p["phase"] == "error" and p["message"] == "Scan error: weird" and p["error"] == "weird"
    assert not sched.is_user_scanning(abs_user.id)


# ---------- check_availability_for_user / all_users ----------

def test_check_availability_for_user_runs_scan(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(sched, "run_scan_for_user", run)
    sched.check_availability_for_user(5)
    run.assert_called_once_with(5)
    assert sched.is_user_scanning(5)  # run_scan_for_user (mocked) is what clears it


def test_check_availability_for_user_skips_when_already_scanning(monkeypatch):
    sched.mark_user_scanning(5)
    run = MagicMock()
    monkeypatch.setattr(sched, "run_scan_for_user", run)
    sched.check_availability_for_user(5)
    run.assert_not_called()


def test_check_availability_all_users_only_abs_users_and_survives_errors(make_user, monkeypatch):
    u1 = make_user("a", abs_base_url="http://1")
    make_user("b")
    u3 = make_user("c", abs_base_url="http://3")
    calls = []

    def fake(user_id):
        calls.append(user_id)
        if user_id == u1.id:
            raise RuntimeError("bad")

    monkeypatch.setattr(sched, "check_availability_for_user", fake)
    sched.check_availability_all_users()
    assert sorted(calls) == sorted([u1.id, u3.id])


# ---------- get_cached_abs_asins ----------

def test_get_cached_abs_asins_missing_returns_none():
    assert sched.get_cached_abs_asins(31337) is None


def test_get_cached_abs_asins_reads_file():
    (sched.DATA_DIR / "abs_asins_5001.json").write_text(json.dumps(["A", "B"]))
    assert sched.get_cached_abs_asins(5001) == {"A", "B"}


def test_get_cached_abs_asins_corrupt_returns_none():
    (sched.DATA_DIR / "abs_asins_5002.json").write_text("{not json")
    assert sched.get_cached_abs_asins(5002) is None


# ---------- reconcile_series_with_cached_asins ----------

def _cache(user_id, asins):
    (sched.DATA_DIR / f"abs_asins_{user_id}.json").write_text(json.dumps(list(asins)))


def test_reconcile_no_cache_returns_zero(session, make_user, make_series, make_book):
    user = make_user()
    series = make_series()
    make_book(series, asin="B1")
    assert sched.reconcile_series_with_cached_asins(session, user.id, series) == 0


def test_reconcile_empty_cache_returns_zero(session, make_user, make_series, make_book):
    user = make_user()
    series = make_series()
    make_book(series, asin="B1")
    _cache(user.id, [])
    assert sched.reconcile_series_with_cached_asins(session, user.id, series) == 0


def test_reconcile_series_without_books(session, make_user, make_series):
    user = make_user()
    _cache(user.id, ["B1"])
    assert sched.reconcile_series_with_cached_asins(session, user.id, make_series()) == 0


def test_reconcile_no_matches(session, make_user, make_series, make_book):
    user = make_user()
    series = make_series()
    make_book(series, asin="B1")
    _cache(user.id, ["ZZZ"])
    assert sched.reconcile_series_with_cached_asins(session, user.id, series) == 0
    assert session.query(UserBookStatus).count() == 0


def test_reconcile_inserts_and_upserts_and_skips_existing(session, make_user, make_series, make_book):
    user = make_user()
    series = make_series()
    new = make_book(series, asin="b0new")  # case-insensitive match
    flip = make_book(series, asin="B0FLIP")
    done = make_book(series, asin="B0DONE")
    noasin = make_book(series, asin="B0MISS")
    session.add_all([
        UserBookStatus(user_id=user.id, book_id=flip.id, in_library=False),
        UserBookStatus(user_id=user.id, book_id=done.id, in_library=True, checked_at=datetime.datetime(2020, 1, 1)),
    ])
    session.commit()
    _cache(user.id, ["B0NEW", "B0FLIP", "B0DONE"])
    assert sched.reconcile_series_with_cached_asins(session, user.id, series) == 2
    session.expire_all()
    st = {s.book_id: s for s in session.query(UserBookStatus).all()}
    assert st[new.id].in_library is True and st[flip.id].in_library is True
    assert st[done.id].checked_at == datetime.datetime(2020, 1, 1)
    assert noasin.id not in st


def test_reconcile_integrity_error_rolls_back(make_user, make_series, make_book):
    user = make_user()
    series = make_series()
    make_book(series, asin="B1")
    _cache(user.id, ["B1"])
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    session.commit.side_effect = IntegrityError("s", {}, Exception("dup"))
    sched.reconcile_series_with_cached_asins(session, user.id, series)
    session.rollback.assert_called_once()


# ---------- send_weekly_digests ----------

@pytest.fixture
def digest_user(make_user, add_push):
    user = make_user("dig", digest_enabled=True)
    add_push(user)
    return user


def test_digest_counts_released_and_new(session, digest_user, make_series, make_book, subscribe, today, push):
    series = make_series()
    subscribe(digest_user, series)
    make_book(series, release_date=today - 3 * DAY)
    make_book(series, release_date=today)
    make_book(series, release_date=today - 30 * DAY)  # old, but created just now => counts as new
    make_book(series, release_date=today + 2 * DAY)
    sched.send_weekly_digests()
    assert push.call_count == 1
    assert push.call_args.kwargs == {
        "title": "Your weekly digest",
        "body": "2 books released · 4 new books added",
        "url": "/",
    }


def test_digest_singular(session, digest_user, make_series, make_book, subscribe, today, push):
    series = make_series()
    subscribe(digest_user, series)
    make_book(series, release_date=today - 60 * DAY, created_at=datetime.datetime.utcnow() - 30 * DAY)
    make_book(series, release_date=today, created_at=datetime.datetime.utcnow() - 30 * DAY)
    sched.send_weekly_digests()
    assert push.call_args.kwargs["body"] == "1 book released"
    push.reset_mock()
    make_book(series, release_date=None)
    sched.send_weekly_digests()
    assert push.call_args.kwargs["body"] == "1 book released · 1 new book added"


def test_digest_skipped_when_nothing_happened(session, digest_user, make_series, make_book, subscribe, today, push):
    series = make_series()
    subscribe(digest_user, series)
    make_book(series, release_date=today - 60 * DAY, created_at=datetime.datetime.utcnow() - 30 * DAY)
    sched.send_weekly_digests()
    push.assert_not_called()


def test_digest_ignores_muted_series_and_disabled_users(session, make_user, add_push, make_series, make_book, subscribe, today, push):
    muted_user = make_user("m", digest_enabled=True)
    add_push(muted_user)
    off_user = make_user("off", digest_enabled=False)
    add_push(off_user)
    series = make_series()
    subscribe(muted_user, series, muted=True)
    subscribe(off_user, series)
    make_book(series, release_date=today)
    sched.send_weekly_digests()
    push.assert_not_called()


def test_digest_deletes_dead_subscriptions(session, digest_user, add_push, make_series, make_book, subscribe, today, push):
    subs = add_push(digest_user)
    dead_endpoint = subs[0].endpoint
    series = make_series()
    subscribe(digest_user, series)
    make_book(series, release_date=today)
    push.side_effect = lambda sub, **kw: sub.endpoint != dead_endpoint
    assert session.query(PushSubscription).count() == 2
    sched.send_weekly_digests()
    session.expire_all()
    remaining = [p.endpoint for p in session.query(PushSubscription).all()]
    assert dead_endpoint not in remaining and len(remaining) == 1


def test_digest_error_for_one_user_does_not_stop_others(session, make_user, add_push, make_series, make_book, subscribe, today, monkeypatch):
    bad = make_user("bad", digest_enabled=True)
    good = make_user("good", digest_enabled=True)
    add_push(bad)
    add_push(good)
    series = make_series()
    subscribe(bad, series)
    subscribe(good, series)
    make_book(series, release_date=today)
    calls = []

    def fake(sub, **kw):
        calls.append(sub.user_id)
        if sub.user_id == bad.id:
            raise RuntimeError("push exploded")
        return True

    monkeypatch.setattr(sched, "send_push", fake)
    sched.send_weekly_digests()
    assert sorted(calls) == sorted([bad.id, good.id])


# ---------- start_scheduler ----------

def test_start_scheduler_registers_three_jobs():
    s = sched.start_scheduler()
    assert isinstance(s, BackgroundScheduler)
    jobs = {j.id: j for j in s.get_jobs()}  # scheduler never started (no-op), so these are pending jobs
    assert set(jobs) == {"refresh_all_series", "check_abs_availability", "weekly_digest"}
    assert jobs["refresh_all_series"].func is sched.refresh_all_series
    assert jobs["check_abs_availability"].func is sched.check_availability_all_users
    assert jobs["weekly_digest"].func is sched.send_weekly_digests
    for j in jobs.values():
        assert j.misfire_grace_time == 3600
    refresh = jobs["refresh_all_series"].trigger
    assert isinstance(refresh, CronTrigger)
    assert str(refresh.timezone) == "America/Chicago"
    fields = {f.name: str(f) for f in refresh.fields}
    assert fields["hour"] == "0,8,16" and fields["minute"] == "0"
    interval = jobs["check_abs_availability"].trigger
    assert isinstance(interval, IntervalTrigger)
    assert interval.interval == datetime.timedelta(hours=6)
    digest = jobs["weekly_digest"].trigger
    assert isinstance(digest, CronTrigger)
    fields = {f.name: str(f) for f in digest.fields}
    assert fields["day_of_week"] == "mon" and fields["hour"] == "13"
    assert str(digest.timezone) == "UTC"
