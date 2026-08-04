import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import get_session
from app.models import Book, Series
from app.scraper import SeriesPageError, fetch_series

logger = logging.getLogger(__name__)


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

        existing_by_asin = {book.asin: book for book in series.books}

        for scraped_book in scraped.books:
            book = existing_by_asin.get(scraped_book.asin)
            if book is None:
                book = Book(series_id=series.id, asin=scraped_book.asin)
                session.add(book)
            book.title = scraped_book.title
            book.position = scraped_book.position
            book.release_date = scraped_book.release_date
            book.url = scraped_book.url
            book.cover_image = scraped_book.image_url

        series.name = scraped.name
        series.last_checked = datetime.datetime.utcnow()
        session.commit()
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
