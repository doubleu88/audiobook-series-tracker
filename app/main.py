import datetime

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    get_current_user,
    get_optional_user,
    get_or_create_session_secret,
    hash_password,
    verify_password,
)
from app.db import get_session, init_db
from app.models import Series, Subscription, User
from app.scheduler import refresh_series, start_scheduler
from app.scraper import SeriesPageError, fetch_series, search_series
from app.timeutil import humanize_relative, shift_months

app = FastAPI(title="Audiobook Series Tracker")
app.add_middleware(SessionMiddleware, secret_key=get_or_create_session_secret())
templates = Jinja2Templates(directory="app/templates")

init_db()
scheduler = start_scheduler()


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if get_optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    session = get_session()
    try:
        user = session.query(User).filter_by(username=username).first()
        if user is None or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "Incorrect username or password."}
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)
    finally:
        session.close()


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    if get_optional_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("signup.html", {"request": request, "error": None})


@app.post("/signup")
def signup(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    session = get_session()
    try:
        if not username or not password:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "Username and password are required."}
            )
        if len(password) < 8:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "Password must be at least 8 characters."}
            )
        if session.query(User).filter_by(username=username).first() is not None:
            return templates.TemplateResponse(
                "signup.html", {"request": request, "error": "That username is already taken."}
            )

        is_first_user = session.query(User).count() == 0

        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
        session.commit()

        if is_first_user:
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
        series_list = (
            session.query(Series)
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

        for series in series_list:
            upcoming = next((b for b in series.books if not b.released), None)
            latest_released = next(
                (b for b in reversed(series.books) if b.released), None
            )
            cover = next((b.cover_image for b in series.books if b.cover_image), None)
            rows.append(
                {
                    "series": series,
                    "upcoming": upcoming,
                    "latest_released": latest_released,
                    "cover": cover,
                }
            )

            for book in series.books:
                if book.release_date is None:
                    continue
                if recent_cutoff <= book.release_date <= today:
                    recent_books.append(
                        {
                            "book": book,
                            "series": series,
                            "relative": humanize_relative((book.release_date - today).days),
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
            scraped = fetch_series(url)
        except SeriesPageError as exc:
            return templates.TemplateResponse(
                "add_series.html", {"request": request, "user": user, "error": str(exc)}
            )

        series = session.query(Series).filter_by(asin=scraped.asin).first()
        if series is None:
            series = Series(asin=scraped.asin, name=scraped.name, url=scraped.url)
            session.add(series)
            session.commit()

        already_subscribed = (
            session.query(Subscription).filter_by(user_id=user.id, series_id=series.id).first()
        )
        if already_subscribed is None:
            session.add(Subscription(user_id=user.id, series_id=series.id))
            session.commit()

        series_id = series.id
    finally:
        session.close()

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
