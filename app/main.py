import csv
import datetime
import io
import secrets
import time

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from icalendar import Calendar, Event
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.audiobookshelf import ABSClient, ABSError
from app.auth import (
    get_current_user,
    get_optional_user,
    get_or_create_session_secret,
    hash_password,
    require_admin,
    verify_password,
)
from app.db import get_session, init_db
from app.models import Book, PushSubscription, Series, Subscription, User, UserBookStatus
from app.prowlarr import ProwlarrClient, ProwlarrError
from app.push import get_vapid_public_key_b64
from app.scheduler import check_availability_for_user, refresh_series, start_scheduler
from app.scraper import SeriesPageError, fetch_series, find_best_match, search_series
from app.timeutil import humanize_relative, shift_months

app = FastAPI(title="Audiobook Series Tracker")
ONE_YEAR_SECONDS = 60 * 60 * 24 * 365
app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_session_secret(),
    max_age=ONE_YEAR_SECONDS,
)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/manifest.json")
def manifest():
    return FileResponse("app/static/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse("app/static/sw.js", media_type="application/javascript")

init_db()
scheduler = start_scheduler()


def _signup_open(session) -> bool:
    return session.query(User).count() == 0


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if get_optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    session = get_session()
    try:
        signup_open = _signup_open(session)
    finally:
        session.close()
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None, "signup_open": signup_open}
    )


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    session = get_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if user is None or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "Incorrect username or password."}
            )
        user.last_login = datetime.datetime.utcnow()
        session.commit()
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)
    finally:
        session.close()


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    if get_optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    session = get_session()
    try:
        if not _signup_open(session):
            return RedirectResponse("/login", status_code=303)
    finally:
        session.close()
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})


@app.post("/signup")
def signup(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    session = get_session()
    try:
        if not _signup_open(session):
            return RedirectResponse("/login", status_code=303)

        if not username or not password:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "Username and password are required."}
            )
        if len(password) < 8:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "Password must be at least 8 characters."}
            )

        # This is the initial-setup account — the only time /signup is reachable.
        # It becomes admin and inherits any series already sitting in the database
        # (e.g. from an older single-user version of this app).
        user = User(
            username=username,
            password_hash=hash_password(password),
            is_admin=True,
            last_login=datetime.datetime.utcnow(),
        )
        session.add(user)
        session.commit()

        unclaimed_series = [s for s in session.query(Series).all() if not s.subscriptions]
        for series in unclaimed_series:
            session.add(Subscription(user_id=user.id, series_id=series.id))
        session.commit()

        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)
    finally:
        session.close()


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account/password", response_class=HTMLResponse)
def account_password_form(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        "account_password.html", {"request": request, "user": user, "error": None, "success": False}
    )


@app.post("/account/password")
def account_password_update(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        error = None
        if not verify_password(current_password, db_user.password_hash):
            error = "Current password is incorrect."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password != confirm_password:
            error = "New password and confirmation don't match."

        if error:
            return templates.TemplateResponse(
                "account_password.html", {"request": request, "user": user, "error": error, "success": False}
            )

        db_user.password_hash = hash_password(new_password)
        session.commit()
        return templates.TemplateResponse(
            "account_password.html", {"request": request, "user": user, "error": None, "success": True}
        )
    finally:
        session.close()


@app.get("/account/export.csv")
def export_subscriptions_csv(user: User = Depends(get_current_user)):
    session = get_session()
    try:
        series_list = (
            session.query(Series)
            .join(Subscription)
            .filter(Subscription.user_id == user.id)
            .order_by(Series.name)
            .all()
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Series", "Status", "URL", "Latest Released", "Next Book", "Next Release Date"])
        for series in series_list:
            latest_released = next((b for b in reversed(series.books) if b.released), None)
            upcoming = next((b for b in series.books if not b.released), None)
            writer.writerow(
                [
                    series.name,
                    "Ended" if series.ended else "Ongoing",
                    series.url,
                    latest_released.title if latest_released else "",
                    upcoming.title if upcoming else "",
                    upcoming.release_date.isoformat() if upcoming and upcoming.release_date else "",
                ]
            )
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audiobook-subscriptions.csv"},
        )
    finally:
        session.close()


@app.get("/calendar/{token}.ics")
def calendar_feed(token: str):
    session = get_session()
    try:
        user = session.query(User).filter_by(calendar_token=token).first()
        if user is None:
            raise HTTPException(status_code=404)

        cal = Calendar()
        cal.add("prodid", "-//Audiobook Series Tracker//")
        cal.add("version", "2.0")
        cal.add("x-wr-calname", "Audiobook Releases")

        series_list = (
            session.query(Series)
            .join(Subscription)
            .filter(Subscription.user_id == user.id, Subscription.muted.is_(False))
            .all()
        )
        for series in series_list:
            for book in series.books:
                if book.release_date is None:
                    continue
                event = Event()
                event.add("summary", f"{series.name}: {book.title}")
                event.add("dtstart", book.release_date)
                event.add("dtend", book.release_date + datetime.timedelta(days=1))
                event.add("uid", f"book-{book.id}@audiobook-tracker")
                event.add("url", book.url)
                cal.add_component(event)

        return Response(content=bytes(cal.to_ical()), media_type="text/calendar")
    finally:
        session.close()


@app.post("/account/digest/toggle")
def toggle_digest(request: Request, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.digest_enabled = not db_user.digest_enabled
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/account/calendar-token/regenerate")
def regenerate_calendar_token(request: Request, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.calendar_token = secrets.token_urlsafe(24)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


def _integrations_context(request: Request, user: User, session, abs_error: str | None = None, prowlarr_error: str | None = None) -> dict:
    db_user = session.get(User, user.id)
    abs_libraries = []
    if db_user.abs_base_url and db_user.abs_api_key and abs_error is None:
        try:
            abs_libraries = ABSClient(db_user.abs_base_url, db_user.abs_api_key).list_libraries()
        except ABSError as exc:
            abs_error = str(exc)

    prowlarr_ok = None
    if db_user.prowlarr_base_url and db_user.prowlarr_api_key and prowlarr_error is None:
        try:
            ProwlarrClient(db_user.prowlarr_base_url, db_user.prowlarr_api_key).test_connection()
            prowlarr_ok = True
        except ProwlarrError as exc:
            prowlarr_error = str(exc)

    return {
        "request": request,
        "user": db_user,
        "abs_libraries": abs_libraries,
        "abs_error": abs_error,
        "prowlarr_error": prowlarr_error,
        "prowlarr_ok": prowlarr_ok,
    }


@app.get("/account/integrations", response_class=HTMLResponse)
def integrations_form(request: Request, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        context = _integrations_context(request, user, session)
    finally:
        session.close()
    return templates.TemplateResponse("integrations.html", context)


@app.post("/account/integrations/audiobookshelf")
def save_audiobookshelf(
    request: Request,
    abs_base_url: str = Form(...),
    abs_api_key: str = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.abs_base_url = abs_base_url.strip().rstrip("/")
        db_user.abs_api_key = abs_api_key.strip()
        session.commit()
        context = _integrations_context(request, user, session)
    finally:
        session.close()
    return templates.TemplateResponse("integrations.html", context)


@app.post("/account/integrations/audiobookshelf/library")
def save_audiobookshelf_library(
    request: Request,
    background_tasks: BackgroundTasks,
    abs_library_id: str = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.abs_library_id = abs_library_id
        session.commit()
    finally:
        session.close()
    background_tasks.add_task(check_availability_for_user, user.id)
    return RedirectResponse("/account/integrations", status_code=303)


@app.post("/account/integrations/audiobookshelf/disconnect")
def disconnect_audiobookshelf(user: User = Depends(get_current_user)):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.abs_base_url = None
        db_user.abs_api_key = None
        db_user.abs_library_id = None
        # Null only the ABS-specific fields, not the whole row — acknowledged/
        # requested_at/last_error must survive disconnecting Audiobookshelf.
        session.query(UserBookStatus).filter_by(user_id=user.id).update(
            {"in_library": False, "checked_at": None}
        )
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/account/integrations", status_code=303)


@app.post("/account/integrations/prowlarr")
def save_prowlarr(
    request: Request,
    prowlarr_base_url: str = Form(...),
    prowlarr_api_key: str = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.prowlarr_base_url = prowlarr_base_url.strip().rstrip("/")
        db_user.prowlarr_api_key = prowlarr_api_key.strip()
        session.commit()
        context = _integrations_context(request, user, session)
    finally:
        session.close()
    return templates.TemplateResponse("integrations.html", context)


@app.post("/account/integrations/prowlarr/disconnect")
def disconnect_prowlarr(user: User = Depends(get_current_user)):
    session = get_session()
    try:
        db_user = session.get(User, user.id)
        db_user.prowlarr_base_url = None
        db_user.prowlarr_api_key = None
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/account/integrations", status_code=303)


@app.post("/account/library-status/refresh")
def refresh_library_status(request: Request, user: User = Depends(get_current_user)):
    check_availability_for_user(user.id)
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    recent_months: int = 3,
    upcoming_months: int = 3,
    user: User = Depends(get_current_user),
):
    recent_months = max(1, min(recent_months, 24))
    upcoming_months = max(1, min(upcoming_months, 24))

    session = get_session()
    try:
        series_rows = (
            session.query(Series, Subscription.muted)
            .join(Subscription)
            .filter(Subscription.user_id == user.id)
            .order_by(Series.name)
            .all()
        )
        rows = []
        today = datetime.date.today()
        recent_cutoff = shift_months(today, -recent_months)
        upcoming_cutoff = shift_months(today, upcoming_months)

        recent_books = []
        upcoming_books = []

        acknowledged_book_ids = {
            row.book_id
            for row in session.query(UserBookStatus.book_id)
            .filter(UserBookStatus.user_id == user.id, UserBookStatus.acknowledged.is_(True))
            .all()
        }

        for series, muted in series_rows:
            upcoming = next((b for b in series.books if not b.released), None)
            latest_released = next(
                (b for b in reversed(series.books) if b.released), None
            )
            released_count = sum(1 for b in series.books if b.released)
            watchlist_count = sum(
                1 for b in series.books if b.released and b.id not in acknowledged_book_ids
            )
            cover = next((b.cover_image for b in series.books if b.cover_image), None)
            rows.append(
                {
                    "series": series,
                    "upcoming": upcoming,
                    "latest_released": latest_released,
                    "released_count": released_count,
                    "watchlist_count": watchlist_count,
                    "cover": cover,
                    "muted": muted,
                }
            )

            if muted:
                continue

            for book in series.books:
                if book.release_date is None:
                    continue
                if recent_cutoff <= book.release_date <= today:
                    recent_books.append(
                        {
                            "book": book,
                            "series": series,
                            "relative": humanize_relative((book.release_date - today).days),
                            "acknowledged": book.id in acknowledged_book_ids,
                        }
                    )
                elif today < book.release_date <= upcoming_cutoff:
                    upcoming_books.append(
                        {
                            "book": book,
                            "series": series,
                            "relative": humanize_relative((book.release_date - today).days),
                        }
                    )

        recent_books.sort(key=lambda r: r["book"].release_date, reverse=True)
        upcoming_books.sort(key=lambda r: r["book"].release_date)

        db_user = session.get(User, user.id)
        abs_connected = bool(db_user.abs_base_url and db_user.abs_library_id)
        prowlarr_connected = bool(db_user.prowlarr_base_url and db_user.prowlarr_api_key)
        if abs_connected or prowlarr_connected:
            statuses = {
                s.book_id: s
                for s in session.query(UserBookStatus)
                .filter(
                    UserBookStatus.user_id == user.id,
                    UserBookStatus.book_id.in_([entry["book"].id for entry in recent_books]),
                )
                .all()
            }
            for entry in recent_books:
                entry["status"] = statuses.get(entry["book"].id)

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "rows": rows,
                "recent_books": recent_books,
                "upcoming_books": upcoming_books,
                "recent_months": recent_months,
                "upcoming_months": upcoming_months,
                "abs_connected": abs_connected,
                "prowlarr_connected": prowlarr_connected,
            },
        )
    finally:
        session.close()


@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request, q: str | None = None, user: User = Depends(get_current_user)):
    results = []
    error = None
    session = get_session()
    try:
        if q:
            try:
                results = search_series(q)
            except Exception as exc:  # noqa: BLE001 - surface any lookup failure to the page
                error = f"Search failed: {exc}"

        subscribed_asins = {
            asin
            for (asin,) in session.query(Series.asin)
            .join(Subscription)
            .filter(Subscription.user_id == user.id)
            .all()
        }
    finally:
        session.close()

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "user": user,
            "q": q or "",
            "results": results,
            "error": error,
            "subscribed_asins": subscribed_asins,
        },
    )


def _subscribe_to_url(session, user: User, url: str) -> int:
    """Finds-or-creates the Series for this URL and ensures a Subscription for
    this user, returning the series id. Raises SeriesPageError if the URL
    doesn't resolve to a scrapeable series page."""
    scraped = fetch_series(url)

    series = session.query(Series).filter_by(asin=scraped.asin).first()
    if series is None:
        series = Series(asin=scraped.asin, name=scraped.name, url=scraped.url)
        session.add(series)
        session.commit()

    already_subscribed = session.query(Subscription).filter_by(user_id=user.id, series_id=series.id).first()
    if already_subscribed is None:
        session.add(Subscription(user_id=user.id, series_id=series.id))
        session.commit()

    return series.id


@app.get("/add", response_class=HTMLResponse)
def add_series_form(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("add_series.html", {"request": request, "user": user, "error": None})


@app.post("/add")
def add_series(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        try:
            series_id = _subscribe_to_url(session, user, url)
        except SeriesPageError as exc:
            return templates.TemplateResponse(
                "add_series.html", {"request": request, "user": user, "error": str(exc)}
            )
    finally:
        session.close()

    background_tasks.add_task(refresh_series, series_id)
    return RedirectResponse("/", status_code=303)


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("import.html", {"request": request, "user": user})


@app.post("/import")
def import_preview(request: Request, lines: str = Form(...), user: User = Depends(get_current_user)):
    raw_lines = [line.strip() for line in lines.splitlines() if line.strip()][:60]

    session = get_session()
    try:
        subscribed_asins = {
            asin
            for (asin,) in session.query(Series.asin)
            .join(Subscription)
            .filter(Subscription.user_id == user.id)
            .all()
        }
    finally:
        session.close()

    results = []
    for index, line in enumerate(raw_lines):
        if index > 0:
            # Audible rate-limits rapid-fire search requests — confirmed by hitting a
            # 503 in testing after a couple of back-to-back calls with no delay.
            time.sleep(1.0)

        try:
            candidates = search_series(line)
        except Exception as exc:  # noqa: BLE001 - surface per-line failures, don't abort the whole batch
            results.append({"query": line, "match": None, "candidates": [], "error": str(exc)})
            continue

        best = find_best_match(line, candidates)
        results.append(
            {
                "query": line,
                "match": best,
                "candidates": candidates[:5],
                "already_subscribed": best is not None and best.asin in subscribed_asins,
                "error": None,
            }
        )

    return templates.TemplateResponse(
        "import_review.html", {"request": request, "user": user, "results": results}
    )


@app.post("/import/confirm")
def import_confirm(
    background_tasks: BackgroundTasks,
    urls: list[str] = Form(default=[]),
    user: User = Depends(get_current_user),
):
    series_ids = []
    session = get_session()
    try:
        for url in urls:
            try:
                series_ids.append(_subscribe_to_url(session, user, url))
            except SeriesPageError:
                continue
    finally:
        session.close()

    for series_id in series_ids:
        background_tasks.add_task(refresh_series, series_id)
    return RedirectResponse("/", status_code=303)


def _require_subscription(session, user: User, series_id: int) -> Subscription | None:
    return session.query(Subscription).filter_by(user_id=user.id, series_id=series_id).first()


@app.post("/series/{series_id}/refresh")
def refresh_one(series_id: int, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        if _require_subscription(session, user, series_id) is None:
            return RedirectResponse("/", status_code=303)
    finally:
        session.close()
    refresh_series(series_id)
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/toggle-ended")
def toggle_ended(series_id: int, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        if _require_subscription(session, user, series_id) is None:
            return RedirectResponse("/", status_code=303)
        series = session.get(Series, series_id)
        if series is not None:
            series.ended = not series.ended
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/toggle-mute")
def toggle_mute(series_id: int, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        subscription = _require_subscription(session, user, series_id)
        if subscription is not None:
            subscription.muted = not subscription.muted
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/unsubscribe")
def unsubscribe(series_id: int, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        subscription = _require_subscription(session, user, series_id)
        if subscription is not None:
            session.delete(subscription)
            session.commit()

            remaining = session.query(Subscription).filter_by(series_id=series_id).count()
            if remaining == 0:
                series = session.get(Series, series_id)
                if series is not None:
                    session.delete(series)
                    session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


def _require_subscription_for_book(session, user: User, book_id: int) -> Book | None:
    """Returns the Book if the user is subscribed to its series, else None. Book has
    no user_id of its own (it's a shared row across subscribers), so every book-scoped
    action needs this same guard the series-scoped actions below already use."""
    book = session.get(Book, book_id)
    if book is None:
        return None
    if _require_subscription(session, user, book.series_id) is None:
        return None
    return book


@app.get("/books/{book_id}/download", response_class=HTMLResponse)
def download_book_form(request: Request, book_id: int, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        book = _require_subscription_for_book(session, user, book_id)
        if book is None:
            return RedirectResponse("/", status_code=303)

        db_user = session.get(User, user.id)
        if not (db_user.prowlarr_base_url and db_user.prowlarr_api_key):
            return RedirectResponse("/account/integrations", status_code=303)

        error = None
        results = []
        try:
            results = ProwlarrClient(db_user.prowlarr_base_url, db_user.prowlarr_api_key).search(book.title)
        except ProwlarrError as exc:
            error = str(exc)

        return templates.TemplateResponse(
            "book_download.html",
            {"request": request, "user": user, "book": book, "results": results, "error": error},
        )
    finally:
        session.close()


@app.post("/books/{book_id}/download/grab")
def download_book_grab(
    book_id: int,
    guid: str = Form(...),
    indexer_id: int = Form(...),
    user: User = Depends(get_current_user),
):
    session = get_session()
    try:
        book = _require_subscription_for_book(session, user, book_id)
        if book is None:
            return RedirectResponse("/", status_code=303)

        db_user = session.get(User, user.id)
        if not (db_user.prowlarr_base_url and db_user.prowlarr_api_key):
            return RedirectResponse("/account/integrations", status_code=303)

        status = session.query(UserBookStatus).filter_by(user_id=user.id, book_id=book.id).first()
        if status is None:
            status = UserBookStatus(user_id=user.id, book_id=book.id)
            session.add(status)

        try:
            ProwlarrClient(db_user.prowlarr_base_url, db_user.prowlarr_api_key).grab(guid, indexer_id)
            status.requested_at = datetime.datetime.utcnow()
            status.last_error = None
        except ProwlarrError as exc:
            status.last_error = str(exc)[:500]
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


def _watchlist_query(session, user: User, series_id: int | None = None, unacknowledged_only: bool = True):
    """Released books in the user's non-muted subscriptions. If unacknowledged_only
    is True, excludes books with an acknowledged UserBookStatus row. Optionally
    scoped to a single series, for the per-series view/bulk-acknowledge."""
    query = (
        session.query(Book, Series)
        .join(Series)
        .join(Subscription, Subscription.series_id == Series.id)
        .filter(
            Subscription.user_id == user.id,
            Subscription.muted.is_(False),
            Book.release_date.isnot(None),
            Book.release_date <= datetime.date.today(),
        )
    )
    if unacknowledged_only:
        acknowledged_book_ids = (
            session.query(UserBookStatus.book_id)
            .filter(UserBookStatus.user_id == user.id, UserBookStatus.acknowledged.is_(True))
            .subquery()
        )
        query = query.filter(Book.id.notin_(acknowledged_book_ids))
    if series_id is not None:
        query = query.filter(Series.id == series_id)
    return query.order_by(Book.release_date.asc())


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, series_id: int | None = None, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        rows = _watchlist_query(session, user, series_id=series_id, unacknowledged_only=False).all()
        acknowledged_book_ids = {
            row.book_id
            for row in session.query(UserBookStatus.book_id)
            .filter(UserBookStatus.user_id == user.id, UserBookStatus.acknowledged.is_(True))
            .all()
        }
        entries = [
            {
                "book": book,
                "series": series,
                "acknowledged": book.id in acknowledged_book_ids,
            }
            for book, series in rows
        ]
        unacknowledged_count = sum(1 for e in entries if not e["acknowledged"])
        filtered_series = None
        if series_id is not None:
            filtered_series = entries[0]["series"] if entries else (
                session.query(Series)
                .join(Subscription)
                .filter(Series.id == series_id, Subscription.user_id == user.id)
                .first()
            )
        return templates.TemplateResponse(
            "watchlist.html",
            {
                "request": request,
                "user": user,
                "entries": entries,
                "unacknowledged_count": unacknowledged_count,
                "filtered_series": filtered_series,
            },
        )
    finally:
        session.close()


def _acknowledge_book(session, user: User, book: Book) -> None:
    status = session.query(UserBookStatus).filter_by(user_id=user.id, book_id=book.id).first()
    if status is None:
        status = UserBookStatus(user_id=user.id, book_id=book.id)
        session.add(status)
    status.acknowledged = True
    status.acknowledged_at = datetime.datetime.utcnow()


def _unacknowledge_book(session, user: User, book: Book) -> None:
    status = session.query(UserBookStatus).filter_by(user_id=user.id, book_id=book.id).first()
    if status is not None:
        status.acknowledged = False
        status.acknowledged_at = None


@app.post("/books/{book_id}/acknowledge")
def acknowledge_book(book_id: int, series_id: int | None = Form(None), user: User = Depends(get_current_user)):
    session = get_session()
    try:
        book = _require_subscription_for_book(session, user, book_id)
        if book is not None:
            _acknowledge_book(session, user, book)
            session.commit()
    finally:
        session.close()
    # Acknowledging one book from a filtered series view should stay on that
    # series so the user can keep clicking through it, not bounce to the
    # full mixed watchlist.
    redirect_url = f"/watchlist?series_id={series_id}" if series_id is not None else "/watchlist"
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/books/{book_id}/unacknowledge")
def unacknowledge_book(book_id: int, series_id: int | None = Form(None), user: User = Depends(get_current_user)):
    session = get_session()
    try:
        book = _require_subscription_for_book(session, user, book_id)
        if book is not None:
            _unacknowledge_book(session, user, book)
            session.commit()
    finally:
        session.close()
    redirect_url = f"/watchlist?series_id={series_id}" if series_id is not None else "/watchlist"
    return RedirectResponse(redirect_url, status_code=303)


@app.post("/watchlist/acknowledge-all")
def acknowledge_all(series_id: int | None = Form(None), user: User = Depends(get_current_user)):
    session = get_session()
    try:
        for book, _series in _watchlist_query(session, user, series_id=series_id).all():
            _acknowledge_book(session, user, book)
        session.commit()
    finally:
        session.close()
    # Unlike acknowledging a single book, this always empties whatever scope
    # it was run in — redirecting back to that (now-empty) filtered view
    # would show a dead end, so always land on the full watchlist instead.
    return RedirectResponse("/watchlist", status_code=303)


@app.get("/admin/health", response_class=HTMLResponse)
def admin_health(request: Request, admin: User = Depends(require_admin)):
    session = get_session()
    try:
        unhealthy = (
            session.query(Series)
            .filter(Series.consecutive_failures > 0)
            .order_by(Series.consecutive_failures.desc())
            .all()
        )
        never_checked = session.query(Series).filter(Series.last_checked.is_(None)).all()
        return templates.TemplateResponse(
            "admin_health.html",
            {"request": request, "user": admin, "unhealthy": unhealthy, "never_checked": never_checked},
        )
    finally:
        session.close()


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, admin: User = Depends(require_admin)):
    session = get_session()
    try:
        users = session.query(User).order_by(User.created_at).all()
        return templates.TemplateResponse(
            "admin_users.html",
            {"request": request, "user": admin, "users": users, "error": None},
        )
    finally:
        session.close()


@app.post("/admin/users")
def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    admin: User = Depends(require_admin),
):
    username = username.strip()
    session = get_session()
    try:
        users = session.query(User).order_by(User.created_at).all()

        if not username or not password:
            error = "Username and password are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif session.query(User).filter_by(username=username).first() is not None:
            error = "That username is already taken."
        else:
            new_user = User(username=username, password_hash=hash_password(password), is_admin=is_admin)
            session.add(new_user)
            session.commit()
            return RedirectResponse("/admin/users", status_code=303)

        return templates.TemplateResponse(
            "admin_users.html",
            {"request": request, "user": admin, "users": users, "error": error},
        )
    finally:
        session.close()


@app.post("/admin/users/{target_user_id}/toggle-admin")
def admin_toggle_admin(target_user_id: int, admin: User = Depends(require_admin)):
    session = get_session()
    try:
        target = session.get(User, target_user_id)
        if target is not None:
            remaining_admins = session.query(User).filter_by(is_admin=True).count()
            if not (target.is_admin and remaining_admins <= 1):
                target.is_admin = not target.is_admin
                session.commit()
    finally:
        session.close()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{target_user_id}/delete")
def admin_delete_user(target_user_id: int, admin: User = Depends(require_admin)):
    session = get_session()
    try:
        if target_user_id != admin.id:
            target = session.get(User, target_user_id)
            if target is not None:
                subscribed_series_ids = [
                    sub.series_id for sub in session.query(Subscription).filter_by(user_id=target.id).all()
                ]
                session.delete(target)
                session.commit()

                for series_id in subscribed_series_ids:
                    remaining = session.query(Subscription).filter_by(series_id=series_id).count()
                    if remaining == 0:
                        series = session.get(Series, series_id)
                        if series is not None:
                            session.delete(series)
                session.commit()
    finally:
        session.close()
    return RedirectResponse("/admin/users", status_code=303)


class PushSubscriptionIn(BaseModel):
    endpoint: str
    keys: dict[str, str]


class PushUnsubscribeIn(BaseModel):
    endpoint: str


@app.get("/push/vapid-public-key")
def vapid_public_key(user: User = Depends(get_current_user)):
    return {"key": get_vapid_public_key_b64()}


@app.post("/push/subscribe")
def push_subscribe(payload: PushSubscriptionIn, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        existing = session.query(PushSubscription).filter_by(endpoint=payload.endpoint).first()
        if existing is None:
            session.add(
                PushSubscription(
                    user_id=user.id,
                    endpoint=payload.endpoint,
                    p256dh=payload.keys["p256dh"],
                    auth=payload.keys["auth"],
                )
            )
        else:
            existing.user_id = user.id
            existing.p256dh = payload.keys["p256dh"]
            existing.auth = payload.keys["auth"]
        session.commit()
    finally:
        session.close()
    return {"ok": True}


@app.post("/push/unsubscribe")
def push_unsubscribe(payload: PushUnsubscribeIn, user: User = Depends(get_current_user)):
    session = get_session()
    try:
        session.query(PushSubscription).filter_by(endpoint=payload.endpoint, user_id=user.id).delete()
        session.commit()
    finally:
        session.close()
    return {"ok": True}
