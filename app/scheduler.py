import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import get_session
from app.models import Book, PushSubscription, Series, Subscription
from app.push import send_push
from app.scraper import SeriesPageError, fetch_series

logger = logging.getLogger(__name__)


def _notify_subscribers(session, series: Series, new_titles: list[str], dated_titles: list[str]) -> None:
    user_ids = [row.user_id for row in session.query(Subscription).filter_by(series_id=series.id).all()]
    if not user_ids:
        return

    messages = []
    if new_titles:
        messages.append(
            f"New book: {new_titles[0]}" if len(new_titles) == 1 else f"{len(new_titles)} new books added"
        )
    if dated_titles:
        if len(dated_titles) == 1:
            messages.append(f"Release date announced for {dated_titles[0]}")
        else:
            messages.append(f"Release dates announced for {len(dated_titles)} books")
    body = " · ".join(messages)

    subscriptions = session.query(PushSubscription).filter(PushSubscription.user_id.in_(user_ids)).all()
    for subscription in subscriptions:
        alive = send_push(subscription, title=series.name, body=body, url="/")
        if not alive:
            session.delete(subscription)
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
            return

        is_first_scrape = series.last_checked is None
        existing_by_asin = {book.asin: book for book in series.books}
        new_titles: list[str] = []
        dated_titles: list[str] = []

        for scraped_book in scraped.books:
            book = existing_by_asin.get(scraped_book.asin)
            if book is None:
                book = Book(series_id=series.id, asin=scraped_book.asin)
                session.add(book)
                if not is_first_scrape:
                    new_titles.append(scraped_book.title)
            elif not is_first_scrape and book.release_date is None and scraped_book.release_date is not None:
                dated_titles.append(scraped_book.title)

            book.title = scraped_book.title
            book.position = scraped_book.position
            book.release_date = scraped_book.release_date
            book.url = scraped_book.url
            book.cover_image = scraped_book.image_url

        series.name = scraped.name
        series.last_checked = datetime.datetime.utcnow()
        session.commit()

        if new_titles or dated_titles:
            _notify_subscribers(session, series, new_titles, dated_titles)
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


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(refresh_all_series, "interval", hours=24, id="refresh_all_series")
    scheduler.start()
    return scheduler
