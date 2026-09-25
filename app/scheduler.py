import datetime
import json
import logging
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.audiobookshelf import ABSClient, ABSError
from app.calendar_feed import event_content_changed, mark_calendar_revision
from app.db import DATA_DIR, get_session
from app.models import Book, BookEdition, PushSubscription, Series, Subscription, User, UserBookStatus
from app.push import send_push
from app.scraper import ScrapedEdition, ScrapedSeries, SeriesPageError, fetch_series, _norm_title_for_group
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

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


def update_series_from_scraped(session, series: Series, scraped: ScrapedSeries) -> None:
    series.consecutive_failures = 0
    series.last_failure_at = None
    series.last_failure_reason = None

    today = datetime.date.today()
    now = datetime.datetime.utcnow()
    is_first_scrape = series.last_checked is None
    series_name_changed = series.name != scraped.name
    existing_by_asin = {book.asin: book for book in series.books if book.asin}
    existing_by_edition_asin: dict[str, list[Book]] = {}
    for book in series.books:
        for ed in book.editions:
            if ed.asin and ed.format_type != "box_set":
                existing_by_edition_asin.setdefault(ed.asin, []).append(book)
    existing_by_title = {
        book.title.lower().strip(): book for book in series.books if book.title
    }
    existing_by_norm_title = {
        _norm_title_for_group(book.title, series.name): book
        for book in series.books
        if book.title
    }
    new_books: list[Book] = []
    dated_books: list[Book] = []
    released_today: list[Book] = []
    matched_book_ids: set[int] = set()

    for scraped_book in scraped.books:
        book = existing_by_asin.get(scraped_book.asin)
        if book is not None and book.id in matched_book_ids:
            book = None
        if book is None and scraped_book.position is not None:
            cand_books = [
                b
                for b in series.books
                if b.position == scraped_book.position and b.id not in matched_book_ids
            ]
            if len(cand_books) > 1 and scraped_book.title:
                norm_s = _norm_title_for_group(scraped_book.title, series.name)
                cand = next(
                    (b for b in cand_books if _norm_title_for_group(b.title, series.name) == norm_s),
                    cand_books[0],
                )
            elif cand_books:
                cand = cand_books[0]
            else:
                cand = None
            if cand is not None:
                book = cand
        if (
            book is None
            and scraped_book.title
            and scraped_book.title.lower().strip() in existing_by_title
        ):
            cand = existing_by_title.get(scraped_book.title.lower().strip())
            if cand is not None and cand.id not in matched_book_ids:
                book = cand
        if book is None and scraped_book.title:
            norm_scraped = _norm_title_for_group(scraped_book.title, series.name)
            cand = existing_by_norm_title.get(norm_scraped)
            if cand is not None and cand.id not in matched_book_ids:
                book = cand
        if book is None and scraped_book.editions:
            for ed in scraped_book.editions:
                if ed.format_type == "box_set":
                    continue
                cands = []
                if ed.asin in existing_by_asin:
                    cands.append(existing_by_asin[ed.asin])
                if ed.asin in existing_by_edition_asin:
                    cands.extend(existing_by_edition_asin[ed.asin])
                for cand in cands:
                    if cand.id not in matched_book_ids:
                        book = cand
                        break
                if book is not None:
                    break

        if book is not None and book.asin != scraped_book.asin:
            logger.info(
                "Updating primary ASIN for series %s book #%s '%s': %s -> %s",
                series.name,
                scraped_book.position,
                scraped_book.title,
                book.asin,
                scraped_book.asin,
            )
            book.asin = scraped_book.asin

        if book is None:
            book = Book(series_id=series.id, asin=scraped_book.asin)
            session.add(book)
            mark_calendar_revision(book, changed=True, now=now)
            if is_first_scrape:
                if scraped_book.release_date is not None and scraped_book.release_date <= today:
                    book.release_day_notified = True
            elif scraped_book.release_date == today:
                released_today.append(book)
            else:
                new_books.append(book)
        else:
            mark_calendar_revision(
                book,
                changed=event_content_changed(
                    book,
                    series_name_changed=series_name_changed,
                    title=scraped_book.title,
                    release_date=scraped_book.release_date,
                    url=scraped_book.url,
                ),
                now=now,
            )
            if not is_first_scrape and book.release_date is None and scraped_book.release_date is not None:
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

        session.flush()  # Ensure book.id is populated for newly inserted books
        matched_book_ids.add(book.id)

        # Synchronize editions for this slot
        existing_editions = {ed.asin: ed for ed in book.editions}
        scraped_ed_asins = {ed.asin for ed in scraped_book.editions}
        if scraped_book.asin not in scraped_ed_asins:
            scraped_book.editions.insert(
                0,
                ScrapedEdition(
                    asin=scraped_book.asin,
                    title=scraped_book.title,
                    format_type="standard",
                    is_primary=True,
                ),
            )
            scraped_ed_asins.add(scraped_book.asin)

        # Remove stale editions no longer associated with this slot
        for ed_asin, old_ed in list(existing_editions.items()):
            if ed_asin not in scraped_ed_asins:
                session.delete(old_ed)
                del existing_editions[ed_asin]

        for ed in scraped_book.editions:
            is_prim = ed.asin == scraped_book.asin
            if ed.asin in existing_editions:
                cur_ed = existing_editions[ed.asin]
                cur_ed.title = ed.title
                cur_ed.sku = ed.sku
                cur_ed.format_type = ed.format_type
                cur_ed.is_primary = is_prim
            else:
                new_ed = BookEdition(
                    book_id=book.id,
                    asin=ed.asin,
                    title=ed.title,
                    sku=ed.sku,
                    format_type=ed.format_type,
                    is_primary=is_prim,
                )
                session.add(new_ed)

    if series_name_changed:
        scraped_asins = {scraped_book.asin for scraped_book in scraped.books}
        for book in series.books:
            if book.asin not in scraped_asins:
                mark_calendar_revision(book, changed=True, now=now)

    # Clean up and merge duplicate/obsolete book rows that have been absorbed into slots
    session.flush()
    active_books_by_id = {b.id: b for b in series.books if b.id in matched_book_ids}
    active_editions_map: dict[str, Book] = {}
    for b in active_books_by_id.values():
        if b.asin:
            active_editions_map[b.asin] = b
    # First map box_set editions (lowest position wins)
    for ed in (
        session.query(BookEdition)
        .filter(BookEdition.book_id.in_(matched_book_ids), BookEdition.format_type == "box_set")
        .all()
    ):
        b = active_books_by_id.get(ed.book_id)
        if ed.asin and b and (
            ed.asin not in active_editions_map
            or (
                b.position is not None
                and (
                    active_editions_map[ed.asin].position is None
                    or b.position < active_editions_map[ed.asin].position
                )
            )
        ):
            active_editions_map[ed.asin] = b
    # Next map non-boxset editions so they take precedence over box sets
    for ed in (
        session.query(BookEdition)
        .filter(BookEdition.book_id.in_(matched_book_ids), BookEdition.format_type != "box_set")
        .all()
    ):
        if ed.asin and ed.book_id in active_books_by_id:
            active_editions_map[ed.asin] = active_books_by_id[ed.book_id]

    for existing in list(series.books):
        if existing.id in matched_book_ids:
            continue

        target_book = active_editions_map.get(existing.asin)
        if target_book is None and existing.position is not None:
            for b in active_books_by_id.values():
                if b.position == existing.position:
                    target_book = b
                    break
        if target_book is None and existing.title:
            norm_ex = _norm_title_for_group(existing.title, series.name)
            for b in active_books_by_id.values():
                if _norm_title_for_group(b.title, series.name) == norm_ex:
                    target_book = b
                    break

        if target_book is not None and target_book.id != existing.id:
            logger.info(
                "Merging duplicate book row '%s' (%s, id=%s) into slot '%s' (%s, id=%s)",
                existing.title,
                existing.asin,
                existing.id,
                target_book.title,
                target_book.asin,
                target_book.id,
            )
            # Merge UserBookStatus from existing to target_book
            for st in session.query(UserBookStatus).filter_by(book_id=existing.id).all():
                target_st = session.query(UserBookStatus).filter_by(user_id=st.user_id, book_id=target_book.id).first()
                if target_st is None:
                    target_st = UserBookStatus(
                        user_id=st.user_id,
                        book_id=target_book.id,
                        in_library=st.in_library,
                        matched_asin=st.matched_asin or existing.asin,
                        checked_at=st.checked_at,
                        requested_at=st.requested_at,
                        last_error=st.last_error,
                        acknowledged=st.acknowledged,
                        acknowledged_at=st.acknowledged_at,
                    )
                    session.add(target_st)
                else:
                    if st.in_library:
                        target_st.in_library = True
                        if not target_st.matched_asin:
                            target_st.matched_asin = st.matched_asin or existing.asin
                    if st.acknowledged:
                        target_st.acknowledged = True
                        target_st.acknowledged_at = target_st.acknowledged_at or st.acknowledged_at
                    if st.requested_at:
                        if not target_st.requested_at or st.requested_at > target_st.requested_at:
                            target_st.requested_at = st.requested_at
                            target_st.last_error = st.last_error
                    elif st.last_error and not target_st.last_error:
                        target_st.last_error = st.last_error
                    if st.checked_at:
                        if not target_st.checked_at or st.checked_at > target_st.checked_at:
                            target_st.checked_at = st.checked_at
                session.delete(st)

            # Re-parent any BookEdition records from existing to target_book
            target_ed_asins = {ed.asin for ed in target_book.editions if ed.asin}
            for ed in list(existing.editions):
                existing.editions.remove(ed)
                if ed.asin and ed.asin not in target_ed_asins:
                    target_book.editions.append(ed)
                    target_ed_asins.add(ed.asin)
                else:
                    session.delete(ed)

            session.delete(existing)
    series.name = scraped.name
    series.last_checked = datetime.datetime.utcnow()
    session.commit()

    if new_books or dated_books:
        _notify_new_and_dated(session, series, new_books, dated_books)
    if released_today:
        _notify_released_today(session, series, released_today)


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

        update_series_from_scraped(session, series, scraped)
        for sub in series.subscriptions:
            try:
                reconcile_series_with_cached_asins(session, sub.user_id, series)
            except Exception:
                logger.warning(
                    "Error reconciling series %s for user %s with cached ASINs",
                    series.id,
                    sub.user_id,
                    exc_info=True,
                )
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


_scanning_users: set[int] = set()
_scan_progress: dict[int, dict] = {}
_scan_lock = threading.Lock()


def is_user_scanning(user_id: int) -> bool:
    with _scan_lock:
        return user_id in _scanning_users


def get_user_scan_progress(user_id: int) -> dict | None:
    with _scan_lock:
        if user_id in _scan_progress:
            return dict(_scan_progress[user_id])
        if user_id in _scanning_users:
            return {"scanning": True, "phase": "starting", "message": "Starting scan...", "percent": 0}
        return None


def mark_user_scanning(user_id: int) -> bool:
    with _scan_lock:
        if user_id in _scanning_users:
            return False
        _scanning_users.add(user_id)
        _scan_progress[user_id] = {
            "scanning": True,
            "phase": "starting",
            "message": "Starting scan...",
            "percent": 5,
            "error": None,
        }
        return True


def run_scan_for_user(user_id: int) -> None:
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None or not (user.abs_base_url and user.abs_api_key and user.abs_library_id):
            with _scan_lock:
                _scan_progress[user_id] = {
                    "scanning": False,
                    "phase": "error",
                    "message": "Audiobookshelf is not configured.",
                    "error": "Not configured",
                }
            return

        logger.info(
            "Starting Audiobookshelf library scan for user '%s' (library: %s)",
            user.username,
            user.abs_library_id,
        )
        with _scan_lock:
            _scan_progress[user_id] = {
                "scanning": True,
                "phase": "fetching",
                "message": "Connecting to Audiobookshelf...",
                "percent": 10,
                "error": None,
            }

        def on_fetch_progress(page: int, total_pages: int | None, items_fetched: int, total_items: int | None) -> None:
            with _scan_lock:
                if total_pages and total_pages > 0:
                    pct = min(90, int((page / total_pages) * 90))
                    msg = f"Fetching library: page {page} of {total_pages} ({items_fetched} ASINs found)..."
                else:
                    pct = min(90, page * 20)
                    msg = f"Fetching library: page {page} ({items_fetched} ASINs found)..."
                _scan_progress[user_id] = {
                    "scanning": True,
                    "phase": "fetching",
                    "page": page,
                    "total_pages": total_pages,
                    "items_fetched": items_fetched,
                    "total_items": total_items,
                    "percent": pct,
                    "message": msg,
                    "error": None,
                }

        t0 = time.time()
        try:
            asins = ABSClient(user.abs_base_url, user.abs_api_key).list_asins_in_library(
                user.abs_library_id, progress_cb=on_fetch_progress
            )
        except ABSError as exc:
            logger.warning("Failed to check Audiobookshelf availability for user %s: %s", user.username, exc)
            with _scan_lock:
                _scan_progress[user_id] = {
                    "scanning": False,
                    "phase": "error",
                    "message": f"Scan failed: {exc}",
                    "error": str(exc),
                }
            return

        fetch_duration = time.time() - t0
        try:
            cache_file = DATA_DIR / f"abs_asins_{user_id}.json"
            cache_file.write_text(json.dumps(list(asins)))
        except Exception:
            logger.exception("Failed to write ABS ASIN cache for user %s", user_id)

        logger.info(
            "Audiobookshelf scan for user '%s': fetched %d ASINs in %.1fs. Reconciling with subscribed books...",
            user.username,
            len(asins),
            fetch_duration,
        )
        with _scan_lock:
            _scan_progress[user_id] = {
                "scanning": True,
                "phase": "matching",
                "percent": 92,
                "message": "Reconciling with subscribed books...",
                "error": None,
            }

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
        checked_count = 0
        already_in_library = 0
        newly_in_library = 0
        not_in_library = 0

        for book in books:
            if not book.released:
                continue
            checked_count += 1
            status = statuses.get(book.id)

            slot_asins = {book.asin.upper()} if book.asin else set()
            for ed in book.editions:
                if ed.asin:
                    slot_asins.add(ed.asin.upper())

            matching = slot_asins.intersection(asins)
            in_library = bool(matching)
            matched_asin = next(iter(matching)) if in_library else None

            if status is not None and status.in_library:
                already_in_library += 1
                status.checked_at = now
                if matched_asin and not status.matched_asin:
                    status.matched_asin = matched_asin
                continue  # already confirmed present; a book later removed from ABS won't un-flip here

            if in_library:
                newly_in_library += 1
            else:
                not_in_library += 1

            if status is None:
                status = UserBookStatus(user_id=user.id, book_id=book.id)
                session.add(status)
                statuses[book.id] = status
            status.in_library = in_library
            status.matched_asin = matched_asin
            status.checked_at = now
        session.commit()
        total_duration = time.time() - t0
        logger.info(
            "Audiobookshelf scan completed for user '%s' in %.1fs: %d released books evaluated (%d already in library, %d newly found, %d not in library)",
            user.username,
            total_duration,
            checked_count,
            already_in_library,
            newly_in_library,
            not_in_library,
        )
        formatted_time = now.strftime("%Y-%m-%d %H:%M UTC")
        with _scan_lock:
            _scan_progress[user_id] = {
                "scanning": False,
                "phase": "complete",
                "percent": 100,
                "message": f"Scan completed: {checked_count} books evaluated ({already_in_library + newly_in_library} in library)",
                "last_scanned_at": formatted_time,
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001 - one bad scan shouldn't crash the scheduler
        logger.exception("Unexpected error during Audiobookshelf scan for user id %s", user_id)
        with _scan_lock:
            _scan_progress[user_id] = {
                "scanning": False,
                "phase": "error",
                "message": f"Scan error: {exc}",
                "error": str(exc),
            }
    finally:
        session.close()
        with _scan_lock:
            _scanning_users.discard(user_id)


def check_availability_for_user(user_id: int) -> None:
    if not mark_user_scanning(user_id):
        logger.info("Audiobookshelf scan already in progress for user id %s; skipping", user_id)
        return
    run_scan_for_user(user_id)


def get_cached_abs_asins(user_id: int) -> set[str] | None:
    cache_file = DATA_DIR / f"abs_asins_{user_id}.json"
    if cache_file.exists():
        try:
            return set(json.loads(cache_file.read_text()))
        except Exception:
            logger.warning("Failed to read ABS ASIN cache %s", cache_file)
    return None


def reconcile_series_with_cached_asins(session, user_id: int, series: Series) -> int:
    cached = get_cached_abs_asins(user_id)
    if not cached:
        return 0
    now = datetime.datetime.utcnow()
    book_ids = [b.id for b in series.books]
    if not book_ids:
        return 0

    existing_in_lib = {
        s.book_id
        for s in session.query(UserBookStatus.book_id)
        .filter(
            UserBookStatus.user_id == user_id,
            UserBookStatus.book_id.in_(book_ids),
            UserBookStatus.in_library.is_(True),
        )
        .all()
    }

    matching_updates: list[tuple[Book, str]] = []
    for b in series.books:
        if b.id in existing_in_lib:
            continue
        slot_asins = {b.asin.upper()} if b.asin else set()
        for ed in b.editions:
            if ed.asin:
                slot_asins.add(ed.asin.upper())
        matched = slot_asins.intersection(cached)
        if matched:
            matching_updates.append((b, next(iter(matched))))

    if not matching_updates:
        return 0

    updated = 0
    try:
        for book, matched_asin in matching_updates:
            stmt = (
                sqlite_insert(UserBookStatus)
                .values(
                    user_id=user_id,
                    book_id=book.id,
                    in_library=True,
                    matched_asin=matched_asin,
                    checked_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["user_id", "book_id"],
                    set_={
                        "in_library": True,
                        "matched_asin": matched_asin,
                        "checked_at": now,
                    },
                )
            )
            session.execute(stmt)
            updated += 1
        session.commit()
    except IntegrityError:
        # A concurrent request or background scan already committed this user's
        # book status records. Rolling back is safe because the database is
        # already up to date with those changes and subsequent reads will see them.
        session.rollback()
        logger.warning(
            "Concurrent commit conflict reconciling cached ASINs for user %s, series %s; "
            "safe to ignore as another transaction already committed the status updates.",
            user_id,
            series.id,
        )
    return updated


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
    scheduler.add_job(
        refresh_all_series,
        CronTrigger(hour="0,8,16", minute=0, timezone="America/Chicago"),
        id="refresh_all_series",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        check_availability_all_users,
        "interval",
        hours=6,
        id="check_abs_availability",
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        send_weekly_digests,
        CronTrigger(day_of_week="mon", hour=13, timezone="UTC"),
        id="weekly_digest",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Background scheduler started (refresh every 8h at 12am/8am/4pm Central, ABS check every 6h, digest Mondays)")
    return scheduler
