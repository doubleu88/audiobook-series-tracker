from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_session, init_db
from app.models import Series
from app.scheduler import refresh_series, start_scheduler
from app.scraper import SeriesPageError, fetch_series, search_series

app = FastAPI(title="Audiobook Series Tracker")
templates = Jinja2Templates(directory="app/templates")

init_db()
scheduler = start_scheduler()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    session = get_session()
    try:
        series_list = session.query(Series).order_by(Series.name).all()
        rows = []
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
        return templates.TemplateResponse(
            "dashboard.html", {"request": request, "rows": rows}
        )
    finally:
        session.close()


@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request, q: str | None = None):
    results = []
    error = None
    if q:
        try:
            results = search_series(q)
        except Exception as exc:  # noqa: BLE001 - surface any lookup failure to the page
            error = f"Search failed: {exc}"

        session = get_session()
        try:
            subscribed_asins = {asin for (asin,) in session.query(Series.asin).all()}
        finally:
            session.close()
    else:
        subscribed_asins = set()

    return templates.TemplateResponse(
        "search.html",
        {
            "request": request,
            "q": q or "",
            "results": results,
            "error": error,
            "subscribed_asins": subscribed_asins,
        },
    )


@app.get("/add", response_class=HTMLResponse)
def add_series_form(request: Request):
    return templates.TemplateResponse("add_series.html", {"request": request, "error": None})


@app.post("/add")
def add_series(request: Request, background_tasks: BackgroundTasks, url: str = Form(...)):
    session = get_session()
    try:
        try:
            scraped = fetch_series(url)
        except SeriesPageError as exc:
            return templates.TemplateResponse(
                "add_series.html", {"request": request, "error": str(exc)}
            )

        existing = session.query(Series).filter_by(asin=scraped.asin).first()
        if existing is None:
            series = Series(asin=scraped.asin, name=scraped.name, url=scraped.url)
            session.add(series)
            session.commit()
            series_id = series.id
        else:
            series_id = existing.id
        session.commit()
    finally:
        session.close()

    background_tasks.add_task(refresh_series, series_id)
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/refresh")
def refresh_one(series_id: int):
    refresh_series(series_id)
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/toggle-ended")
def toggle_ended(series_id: int):
    session = get_session()
    try:
        series = session.get(Series, series_id)
        if series is not None:
            series.ended = not series.ended
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)


@app.post("/series/{series_id}/delete")
def delete_series(series_id: int):
    session = get_session()
    try:
        series = session.get(Series, series_id)
        if series is not None:
            session.delete(series)
            session.commit()
    finally:
        session.close()
    return RedirectResponse("/", status_code=303)
