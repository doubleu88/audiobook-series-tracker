import datetime
import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.audiobookshelf import ABSClient, ABSError
from app.db import get_session
from app.models import Book, PushSubscription, Series, Subscription, User, UserBookStatus
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

        # Audible's WAF is flaky in a way that isn't purely rate-limit-shaped —
        # the same ASIN can fail outright while others succeed at the same
        # delay, and vice versa (confirmed empirically: retrying a "failed"
        # series standalone moments later succeeds with no code change). A
        # few retries with a real gap between them clears most of these
        # without ever marking the series as actually broken.
        scraped = None
        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(3.0)
            try:
                scraped = fetch_series(series.url)
                break
            except SeriesPageError as exc:
                last_exc = exc
                logger.debug("Attempt %d/3 to refresh %s failed: %s", attempt + 1, series.name, exc)
            except Exception as exc:  # noqa: BLE001 - retry, then log and move on
                last_exc = exc
                logger.exception("Unexpected error on attempt %d/3 refreshing %s", attempt + 1, series.name)

        if scraped is None:
            logger.warning("Failed to refresh series %s (%s): %s", series.name, series.asin, last_exc)
            series.consecutive_failures += 1
            series.last_failure_at = datetime.datetime.utcnow()
            series.last_failure_reason = str(last_exc)[:500]
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

    for index, series_id in enumerate(series_ids):
        if index > 0:
            # Audible rate-limits rapid-fire requests — same reasoning as the
            # sleep in main.py's bulk import flow. Without this, refreshing a
            # full subscription list back-to-back starts intermittently
            # failing partway through with a generic "No books found" error
            # that has nothing to do with the actual page content.
            time.sleep(1.0)
        try:
            refresh_series(series_id)
        except Exception:  # noqa: BLE001 - one bad series shouldn't stop the rest of the batch
            logger.exception("Unexpected error refreshing series id %s", series_id)


def check_availability_for_user(user_id: int) -> None:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None or not (user.abs_base_url and user.abs_api_key and user.abs_library_id):
            return

        try:
            asins = ABSClient(user.abs_base_url, user.abs_api_key).list_asins_in_library(user.abs_library_id)
        except ABSError as exc:
            logger.warning("Failed to check Audiobookshelf availability for user %s: %s", user.username, exc)
            return

        books = (
            session.query(Book)
            .join(Series)
            .join(Subscription)
            .filter(Subscription.user_id == user.id, Book.release_date.isnot(None))
            .all()
        )
        statuses = {
            s.book_id: s
            for s in session.query(UserBookStatus).filter_by(user_id=user.id).all()
        }
        now = datetime.datetime.utcnow()
        for book in books:
            if not book.released:
                continue
            status = statuses.get(book.id)
            if status is not None and status.in_library:
                continue  # already confirmed present; a book later removed from ABS won't un-flip here
            in_library = book.asin.upper() in asins
            if status is None:
                status = UserBookStatus(user_id=user.id, book_id=book.id)
                session.add(status)
            status.in_library = in_library
            status.checked_at = now
        session.commit()
    finally:
        session.close()


def check_availability_all_users() -> None:
    session = get_session()
    try:
        user_ids = [
            u.id for u in session.query(User).filter(User.abs_base_url.isnot(None)).all()
        ]
    finally:
        session.close()

    for user_id in user_ids:
        try:
            check_availability_for_user(user_id)
        except Exception:  # noqa: BLE001 - one bad user shouldn't stop the rest of the batch
            logger.exception("Unexpected error checking Audiobookshelf availability for user id %s", user_id)


def send_weekly_digests() -> None:
    session = get_session()
    try:
        today = datetime.date.today()
        week_ago_date = today - datetime.timedelta(days=7)
        week_ago_datetime = datetime.datetime.utcnow() - datetime.timedelta(days=7)

        for user in session.query(User).filter_by(digest_enabled=True).all():
            try:
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
            except Exception:  # noqa: BLE001 - one bad user shouldn't stop everyone else's digest
                logger.exception("Unexpected error building weekly digest for user %s", user.username)
        session.commit()
    finally:
        session.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_all_series, "interval", hours=24, id="refresh_all_series")
    scheduler.add_job(
        check_availability_all_users, "interval", hours=6, id="check_abs_availability"
    )
    scheduler.add_job(
        send_weekly_digests, CronTrigger(day_of_week="mon", hour=13, timezone="UTC"), id="weekly_digest"
    )
    scheduler.start()
    logger.info("Background scheduler started (refresh every 24h, ABS check every 6h, digest Mondays)")
    return scheduler
