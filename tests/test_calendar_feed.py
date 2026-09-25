import datetime
import unittest
from types import SimpleNamespace

from icalendar import Calendar
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.calendar_feed import calendar_feed_response, render_calendar
from app.db import _migrate
from app.models import Base, Book, Series
from app.scheduler import update_series_from_scraped
from app.scraper import ScrapedBook, ScrapedSeries


def _book(**overrides):
    values = dict(
        id=1,
        title="Wind and Truth",
        release_date=datetime.date(2026, 12, 6),
        url="https://www.audible.com/pd/Wind-and-Truth/B0TEST0001",
        created_at=datetime.datetime(2026, 1, 1, 12, 0, 0),
        updated_at=datetime.datetime(2026, 1, 2, 12, 0, 0),
        ics_sequence=1,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _series(books, name="The Stormlight Archive"):
    return SimpleNamespace(name=name, books=books)


def _events(body: bytes):
    return Calendar.from_ical(body).walk("VEVENT")


class CalendarFeedTests(unittest.TestCase):
    def test_feed_is_stable_and_carries_google_revision_fields(self):
        series = [_series([
            _book(url="https://www.audible.com/pd/The-Way-of-Kings-Audiobook/B003ZWFO7E?qid=1234567890&sr=1-1&ref=a_search_c3_lProduct_1_1&pf_rd_p=83218cca-c308-412f-bfcf-90198b687a2f")
        ])]
        first, revised = render_calendar(series)
        second, _ = render_calendar(series)
        self.assertEqual(first, second)
        self.assertEqual(revised, datetime.datetime(2026, 1, 2, 12, 0, tzinfo=datetime.timezone.utc))

        text_body = first.decode()
        self.assertIn("METHOD:PUBLISH", text_body)
        self.assertIn("CALSCALE:GREGORIAN", text_body)
        self.assertIn("REFRESH-INTERVAL;VALUE=DURATION:PT1H", text_body)
        self.assertIn("X-PUBLISHED-TTL:PT1H", text_body)
        for line in first.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)

        event = _events(first)[0]
        self.assertEqual(str(event["uid"]), "book-1@audiobook-tracker")
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(event.decoded("dtstart"), datetime.date(2026, 12, 6))
        self.assertEqual(event.decoded("dtend"), datetime.date(2026, 12, 7))
        self.assertEqual(event.decoded("dtstamp"), event.decoded("last-modified"))
        self.assertIn("DTSTAMP:20260102T120000Z", text_body)
        self.assertEqual(str(event["status"]), "CONFIRMED")
        self.assertEqual(str(event["transp"]), "TRANSPARENT")

    def test_books_without_a_date_are_omitted_and_bad_urls_are_not_emitted(self):
        undated = _book(id=2, release_date=None)
        bad_url = _book(id=3, url="not a url")
        body, _ = render_calendar([_series([undated, bad_url])])
        events = _events(body)
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0]["uid"]), "book-3@audiobook-tracker")
        self.assertNotIn("URL", events[0])

    def test_summary_special_characters_are_escaped(self):
        book = _book(title="Part One, Knights; Radiant")
        body, _ = render_calendar([_series([book], name="Archive: Main")])
        event = _events(body)[0]
        self.assertEqual(str(event["summary"]), "Archive: Main: Part One, Knights; Radiant")

    def test_revision_bump_moves_sequence_and_dtstamp_forward(self):
        book = _book()
        before, _ = render_calendar([_series([book])])
        book.ics_sequence = 2
        book.updated_at = datetime.datetime(2026, 3, 1, 8, 0, 0)
        after, _ = render_calendar([_series([book])])
        self.assertNotEqual(before, after)
        old = _events(before)[0]
        new = _events(after)[0]
        self.assertGreater(new["sequence"], old["sequence"])
        self.assertGreater(new.decoded("dtstamp"), old.decoded("dtstamp"))
        self.assertEqual(str(new["uid"]), str(old["uid"]))

    def test_response_headers_let_subscribers_revalidate(self):
        response = calendar_feed_response([_series([_book()])])
        self.assertEqual(response.headers["content-type"], "text/calendar; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "max-age=3600, must-revalidate")
        self.assertTrue(response.headers["etag"].startswith('"'))
        self.assertIn("audiobook-releases.ics", response.headers["content-disposition"])
        self.assertIn("GMT", response.headers["last-modified"])
        again = calendar_feed_response([_series([_book()])])
        self.assertEqual(response.headers["etag"], again.headers["etag"])


class CalendarRevisionTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        self.series = Series(asin="B0SERIES001", name="Archive", url="https://www.audible.com/series/B0SERIES001")
        self.session.add(self.series)
        self.session.commit()
        self.book = Book(
            series_id=self.series.id,
            asin="B0BOOK00001",
            title="Wind and Truth",
            url="https://www.audible.com/pd/Wind-and-Truth/B0BOOK00001",
            release_date=datetime.date(2026, 12, 6),
            position=1,
            ics_sequence=1,
            updated_at=datetime.datetime(2026, 1, 2, 12, 0, 0),
            created_at=datetime.datetime(2026, 1, 1, 12, 0, 0),
        )
        self.session.add(self.book)
        self.series.last_checked = datetime.datetime(2026, 1, 2, 12, 0, 0)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _scraped(self, books, name="Archive"):
        return ScrapedSeries(asin="B0SERIES001", name=name, url=self.series.url, books=books)

    def _scraped_book(self, **overrides):
        values = dict(
            asin="B0BOOK00001",
            title="Wind and Truth",
            position=1.0,
            release_date=datetime.date(2026, 12, 6),
            url="https://www.audible.com/pd/Wind-and-Truth/B0BOOK00001",
            image_url=None,
        )
        values.update(overrides)
        return ScrapedBook(**values)

    def test_unchanged_scrape_does_not_bump_revision(self):
        update_series_from_scraped(self.session, self.series, self._scraped([self._scraped_book()]))
        self.session.refresh(self.book)
        self.assertEqual(self.book.ics_sequence, 1)
        self.assertEqual(self.book.updated_at, datetime.datetime(2026, 1, 2, 12, 0, 0))

    def test_new_release_date_and_new_book_publish_a_higher_revision(self):
        undated = Book(
            series_id=self.series.id,
            asin="B0BOOK00002",
            title="Untitled",
            url="https://www.audible.com/pd/Untitled/B0BOOK00002",
            release_date=None,
            ics_sequence=1,
            updated_at=datetime.datetime(2026, 1, 2, 12, 0, 0),
        )
        self.session.add(undated)
        self.session.commit()

        update_series_from_scraped(
            self.session,
            self.series,
            self._scraped(
                [
                    self._scraped_book(title="Wind and Truth, Revised"),
                    self._scraped_book(
                        asin="B0BOOK00002",
                        title="Untitled",
                        release_date=datetime.date(2027, 1, 15),
                        url="https://www.audible.com/pd/Untitled/B0BOOK00002",
                    ),
                    self._scraped_book(
                        asin="B0BOOK00003",
                        title="Brand New",
                        release_date=datetime.date(2027, 6, 1),
                        url="https://www.audible.com/pd/Brand-New/B0BOOK00003",
                        position=3.0,
                    ),
                ]
            ),
        )
        self.session.refresh(self.book)
        self.session.refresh(undated)
        added = self.session.query(Book).filter_by(asin="B0BOOK00003").one()
        self.assertEqual(self.book.ics_sequence, 2)
        self.assertGreater(self.book.updated_at, datetime.datetime(2026, 1, 2, 12, 0, 0))
        self.assertEqual(undated.ics_sequence, 2)
        self.assertEqual(undated.release_date, datetime.date(2027, 1, 15))
        self.assertEqual(added.ics_sequence, 1)
        self.assertIsNotNone(added.updated_at)

        body, _ = render_calendar([self.series])
        uids = {str(event["uid"]) for event in _events(body)}
        self.assertIn(f"book-{added.id}@audiobook-tracker", uids)
        self.assertIn(f"book-{undated.id}@audiobook-tracker", uids)


class MigrationTests(unittest.TestCase):
    def test_existing_books_get_a_forward_revision(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE books (
                        id INTEGER PRIMARY KEY,
                        title VARCHAR,
                        created_at DATETIME
                    )
                    """
                )
            )
            conn.execute(text("INSERT INTO books (title, created_at) VALUES ('A', '2020-01-01 00:00:00')"))
            conn.execute(text("CREATE TABLE series (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, created_at DATETIME)"))
            _migrate(conn)
            sequence, updated_at, created_at = conn.execute(
                text("SELECT ics_sequence, updated_at, created_at FROM books")
            ).one()
        self.assertEqual(sequence, 1)
        self.assertNotEqual(updated_at, created_at)
        self.assertIsNotNone(updated_at)


if __name__ == "__main__":
    unittest.main()
