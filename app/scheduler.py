import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import get_session
from app.models import Book, PushSubscription, Series, Subscription, User
from app.push import send_push
from app.scraper import SeriesPageError, fetch_series

logger = logging.getLogger(__name__)


def _pick_icon(books: list[Book]) -> str | None:
    for book in books:
        if book.cover_image:
            return book.cover_image
    return None


def _push_to_series_subscribers(session, series: Series, body: str, icon: str | None) -> None:
    user_ids = [
        row.user_id
        for row in session.query(Subscription).filter_by(series_id=series.id, muted=False).all()
    ]
    if not user_ids:
        return

    subscriptions = session.query(PushSubscription).filter(PushSubscription.user_id.in_(user_ids)).all()
    for subscription in subscriptions:
        alive = send_push(subscription, title=series.name, body=body, url="/", icon=icon)
        if not alive:
            session.delete(subscription)
    session.commit()


def _notify_new_and_dated(session, series: Series, new_books: list[Book], dated_books: list[Book]) -> None:
    messages = []
    if new_books:
        messages.append(
            f"New book: {new_books[0].title}" if len(new_books) == 1 else f"{len(new_books)} new books added"
        )
    if dated_books:
        if len(dated_books) == 1:
            messages.append(f"Release date announced for {dated_books[0].title}")
        else:
            messages.append(f"Release dates announced for {len(dated_books)} books")

    body = " · ".join(messages)
    icon = _pick_icon(new_books + dated_books)
    _push_to_series_subscribers(session, series, body, icon)


def _notify_released_today(session, series: Series, released_books: list[Book]) -> None:
    if len(released_books) == 1:
        body = f"🎉 {released_books[0].title} is out today!"
    else:
        body = f"🎉 {len(released_books)} books are out today!"

    icon = _pick_icon(released_books)
    _push_to_series_subscribers(session, series, body, icon)

    for book in released_books:
        book.release_day_notified = True
    session.commit()


def refresh_series(series_id: int) -> None:
    session = get_session()
    try:
        series = session.get(Series, series_id)
        if series is None:
            return

        try:
            scraped = fetch_series(series.url)
        except (SeriesPageError, Exception) as exc:  # noqa: BLE001 - log and move on, don't crash the poll loop
            logger.warning("Failed to refresh series %s (%s): %s", series.name, series.asin, exc)
            series.consecutive_failures += 1
            series.last_failure_at = datetime.datetime.utcnow()
            series.last_failure_reason = str(exc)[:500]
            session.commit()
            return

        series.consecutive_failures = 0
        series.last_failure_at = None
        series.last_failure_reason = None

        today = datetime.date.today()
        is_first_scrape = series.last_checked is None
        existing_by_asin = {book.asin: book for book in series.books}
        new_books: list[Book] = []
        dated_books: list[Book] = []
        released_today: list[Book] = []

        for scraped_book in scraped.books:
            book = existing_by_asin.get(scraped_book.asin)
            if book is None:
                book = Book(series_id=series.id, asin=scraped_book.asin)
                session.add(book)
                if is_first_scrape:
                    # Already out (today or earlier) at subscribe time — mark it as
                    # accounted for so it doesn't fire a stale "released today" the
                    # next time this series is refreshed.
                    if scraped_book.release_date is not None and scraped_book.release_date <= today:
                        book.release_day_notified = True
                elif scraped_book.release_date == today:
                    released_today.append(book)
                else:
                    new_books.append(book)
            elif not is_first_scrape and book.release_date is None and scraped_book.release_date is not None:
                if scraped_book.release_date == today:
                    released_today.append(book)
                else:
                    dated_books.append(book)
            elif (
                not is_first_scrape
                and book.release_date == today
                and scraped_book.release_date == today
                and not book.release_day_notified
            ):
                released_today.append(book)

            book.title = scraped_book.title
            book.position = scraped_book.position
            book.release_date = scraped_book.release_date
            book.url = scraped_book.url
            book.cover_image = scraped_book.image_url

        series.name = scraped.name
        series.last_checked = datetime.datetime.utcnow()
        session.commit()

        if new_books or dated_books:
            _notify_new_and_dated(session, series, new_books, dated_books)
        if released_today:
            _notify_released_today(session, series, released_today)
    finally:
        session.close()


def refresh_all_series() -> None:
    session = get_session()
    try:
        series_ids = [s.id for s in session.query(Series).all()]
    finally:
        session.close()

    for series_id in series_ids:
        refresh_series(series_id)


def send_weekly_digests() -> None:
    session = get_session()
    try:
        today = datetime.date.today()
        week_ago_date = today - datetime.timedelta(days=7)
        week_ago_datetime = datetime.datetime.utcnow() - datetime.timedelta(days=7)

        for user in session.query(User).filter_by(digest_enabled=True).all():
            series_list = (
                session.query(Series)
                .join(Subscription)
                .filter(Subscription.user_id == user.id, Subscription.muted.is_(False))
                .all()
            )

            new_count = 0
            released_count = 0
            for series in series_list:
                for book in series.books:
                    if book.created_at and book.created_at >= week_ago_datetime:
                        new_count += 1
                    if book.release_date and week_ago_date <= book.release_date <= today:
                        released_count += 1

            if new_count == 0 and released_count == 0:
                continue

            parts = []
            if released_count:
                parts.append(f"{released_count} book{'s' if released_count != 1 else ''} released")
            if new_count:
                parts.append(f"{new_count} new book{'s' if new_count != 1 else ''} added")
            body = " · ".join(parts)

            subscriptions = session.query(PushSubscription).filter_by(user_id=user.id).all()
            for subscription in subscriptions:
                alive = send_push(subscription, title="Your weekly digest", body=body, url="/")
                if not alive:
                    session.delete(subscription)
        session.commit()
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_all_series, "interval", hours=24, id="refresh_all_series")
    scheduler.add_job(
        send_weekly_digests, CronTrigger(day_of_week="mon", hour=13, timezone="UTC"), id="weekly_digest"
    )
    scheduler.start()
    return scheduler
