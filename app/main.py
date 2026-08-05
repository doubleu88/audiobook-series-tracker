import datetime

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    get_current_user,
    get_optional_user,
    get_or_create_session_secret,
    hash_password,
    require_admin,
    verify_password,
)
from app.db import get_session, init_db
from app.models import PushSubscription, Series, Subscription, User
from app.push import get_vapid_public_key_b64
from app.scheduler import refresh_series, start_scheduler
from app.scraper import SeriesPageError, fetch_series, search_series
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
        user = User(username=username, password_hash=hash_password(password), is_admin=True)
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
