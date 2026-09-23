"""Personal iCalendar feed of audiobook release dates.

Google Calendar's subscription importer keeps a server-side copy of every
event, keyed by UID. It applies a later fetch only when SEQUENCE is higher
than the stored value and DTSTAMP / LAST-MODIFIED are not older. A feed that
restamps every event with "now" on each request, and never raises SEQUENCE,
is treated as stale: the first subscribe imports whatever is there, and
releases added afterwards never show up.

DTSTAMP is therefore the book's revision time, not the request time, and
SEQUENCE starts at 1 (above the implicit 0 Google stored from older feeds)
and increases only when the event's visible fields change.
"""

import datetime
import hashlib
from email.utils import format_datetime
from urllib.parse import urlparse

from fastapi.responses import Response
from icalendar import Calendar, Event
from icalendar.prop import vDuration

_REFRESH_EVERY = datetime.timedelta(hours=1)


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _revision_stamp(book) -> datetime.datetime:
    stamp = getattr(book, "updated_at", None) or getattr(book, "created_at", None)
    if stamp is None:
        stamp = datetime.datetime(1970, 1, 1)
    return _as_utc(stamp).replace(microsecond=0)


def event_content_changed(
    book,
    *,
    series_name_changed: bool,
    title: str,
    release_date: datetime.date | None,
    url: str | None,
) -> bool:
    """True when a scrape changed something the calendar event displays."""
    if series_name_changed:
        return True
    return (
        book.title != title
        or book.release_date != release_date
        or (book.url or "") != (url or "")
    )


def mark_calendar_revision(book, *, changed: bool, now: datetime.datetime) -> None:
    """Advance the revision Google Calendar uses to accept an update.

    A row that has never been published starts at sequence 1. Later edits
    increment it and move updated_at forward so DTSTAMP cannot go backwards.
    """
    if not book.ics_sequence:
        book.ics_sequence = 1
        book.updated_at = now
        return
    if changed:
        book.ics_sequence = int(book.ics_sequence) + 1
        book.updated_at = now


def _https_url(url: str | None) -> str | None:
    if not url:
        return None
    candidate = url.strip()
    if any(char in candidate for char in "\r\n\t "):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _add_refresh_interval(cal: Calendar) -> None:
    refresh = vDuration(_REFRESH_EVERY)
    refresh.params["VALUE"] = "DURATION"
    cal.add("refresh-interval", refresh)
    cal.add("x-published-ttl", vDuration(_REFRESH_EVERY))


def render_calendar(series_list) -> tuple[bytes, datetime.datetime | None]:
    """Return the ICS document and the newest event revision, if any."""
    cal = Calendar()
    cal.add("prodid", "-//Audiobook Series Tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("name", "Audiobook Releases")
    cal.add("x-wr-calname", "Audiobook Releases")
    _add_refresh_interval(cal)

    rows = []
    for series in series_list:
        for book in series.books:
            if book.release_date is None or book.id is None:
                continue
            rows.append((book.release_date, (series.name or "").casefold(), book.id, series, book))
    rows.sort(key=lambda row: (row[0], row[1], row[2]))

    newest: datetime.datetime | None = None
    for _release_date, _name, _book_id, series, book in rows:
        revised = _revision_stamp(book)
        if newest is None or revised > newest:
            newest = revised
        created = _as_utc(book.created_at).replace(microsecond=0) if book.created_at else revised

        event = Event()
        event.add("uid", f"book-{book.id}@audiobook-tracker")
        event.add("summary", f"{series.name}: {book.title}")
        event.add("dtstamp", revised)
        event.add("created", created)
        event.add("last-modified", revised)
        event.add("sequence", int(book.ics_sequence or 1))
        event.add("dtstart", book.release_date)
        event.add("dtend", book.release_date + datetime.timedelta(days=1))
        event.add("status", "CONFIRMED")
        event.add("transp", "TRANSPARENT")
        url = _https_url(book.url)
        if url is not None:
            event.add("url", url)
        cal.add_component(event)

    return cal.to_ical(), newest


def calendar_feed_response(series_list) -> Response:
    body, revised_at = render_calendar(series_list)
    headers = {
        "Content-Disposition": 'inline; filename="audiobook-releases.ics"',
        # Explicit freshness so a proxy cannot heuristically cache the first
        # fetch forever. One hour matches how often we ask clients to refresh;
        # Google still polls on its own longer schedule.
        "Cache-Control": "max-age=3600, must-revalidate",
        "ETag": f'"{hashlib.sha256(body).hexdigest()}"',
    }
    if revised_at is not None:
        headers["Last-Modified"] = format_datetime(revised_at, usegmt=True)
    return Response(content=body, media_type="text/calendar; charset=utf-8", headers=headers)
