import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Book, PushSubscription, Series, Subscription, User, UserBookStatus


def test_user_defaults(make_user):
    u = make_user("bob")
    assert u.is_admin is False
    assert u.digest_enabled is False
    assert u.last_login is None
    assert isinstance(u.created_at, datetime.datetime)
    assert u.abs_base_url is None and u.prowlarr_api_key is None


def test_calendar_tokens_unique_and_generated(make_user):
    a, b = make_user("a"), make_user("b")
    assert a.calendar_token and b.calendar_token and a.calendar_token != b.calendar_token
    assert len(a.calendar_token) >= 24


def test_username_unique(session, make_user):
    make_user("dup")
    with pytest.raises(IntegrityError):
        make_user("dup")


def test_series_defaults_and_asin_unique(session, make_series):
    s = make_series(asin="B0DUP")
    assert s.ended is False and s.consecutive_failures == 0
    assert s.last_checked is None and s.last_failure_at is None and s.last_failure_reason is None
    with pytest.raises(IntegrityError):
        make_series(asin="B0DUP")


def test_book_defaults(make_series, make_book):
    b = make_book(make_series())
    assert b.release_day_notified is False
    assert b.cover_image is None
    assert isinstance(b.created_at, datetime.datetime)


def test_book_released_property(make_series, make_book, today):
    s = make_series()
    day = datetime.timedelta(days=1)
    assert make_book(s, release_date=None).released is False
    assert make_book(s, release_date=today - day).released is True
    assert make_book(s, release_date=today).released is True
    assert make_book(s, release_date=today + day).released is False


def test_series_books_ordered_by_position(session, make_series, make_book):
    s = make_series()
    make_book(s, position=3)
    make_book(s, position=1)
    make_book(s, position=2)
    session.refresh(s)
    assert [b.position for b in s.books] == [1, 2, 3]


def test_subscription_unique_user_series(session, make_user, make_series, subscribe):
    u, s = make_user(), make_series()
    sub = subscribe(u, s)
    assert sub.muted is False
    with pytest.raises(IntegrityError):
        subscribe(u, s)


def test_subscription_same_series_different_users_ok(make_user, make_series, subscribe):
    s = make_series()
    subscribe(make_user("a"), s)
    subscribe(make_user("b"), s)


def test_subscription_relationships(make_user, make_series, subscribe):
    u, s = make_user(), make_series()
    sub = subscribe(u, s)
    assert sub.user is u or sub.user.id == u.id
    assert sub.series.id == s.id


def test_delete_series_cascades(session, make_user, make_series, make_book, subscribe):
    u, s = make_user(), make_series()
    make_book(s)
    subscribe(u, s)
    session.delete(s)
    session.commit()
    assert session.query(Book).count() == 0
    assert session.query(Subscription).count() == 0
    assert session.query(User).count() == 1


def test_push_subscription(session, make_user):
    u = make_user()
    p = PushSubscription(user_id=u.id, endpoint="https://e/1", p256dh="k", auth="a")
    session.add(p)
    session.commit()
    assert isinstance(p.created_at, datetime.datetime)
    assert p.user.id == u.id
    session.add(PushSubscription(user_id=u.id, endpoint="https://e/1", p256dh="k", auth="a"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_user_book_status_defaults_and_unique(session, make_user, make_series, make_book):
    u, b = make_user(), make_book(make_series())
    st = UserBookStatus(user_id=u.id, book_id=b.id)
    session.add(st)
    session.commit()
    assert st.in_library is False and st.acknowledged is False
    assert st.checked_at is None and st.requested_at is None
    assert st.last_error is None and st.acknowledged_at is None
    assert st.book.id == b.id and st.user.id == u.id
    session.add(UserBookStatus(user_id=u.id, book_id=b.id))
    with pytest.raises(IntegrityError):
        session.commit()
