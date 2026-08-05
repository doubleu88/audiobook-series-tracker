# 🎧 Audiobook Series Tracker

A self-hosted app for tracking audiobook series on Audible. Subscribe to the
series you listen to and it tells you when the next book is coming out — or
whether the series has wrapped up — without you having to go check manually.

Audible has no "notify me about the next book" feature and no public API for
this, so this app scrapes Audible's own public series and search pages (which
`robots.txt` explicitly permits crawling) for book titles, positions, release
dates, and cover art.

## Features

- **Multi-user accounts with an admin role** — each person logs in and
  subscribes to their own set of series. If two users subscribe to the same
  series, it's only scraped and stored once and shared between them;
  unsubscribing just removes your own subscription (the series sticks around
  until its last subscriber drops it). The first account ever created becomes
  admin and can create/promote/delete other users from `/admin/users`; after
  that, public signup closes — there's always at least one admin, so you
  can't lock yourself out.
- **Search Audible directly** to subscribe — no need to hunt down a series URL
  yourself (pasting a URL/ASIN is also supported as a fallback).
- **Recently released** and **releasing soon** sections on the dashboard,
  each a rolling window (default 3 months, adjustable per-section) across
  every series you're subscribed to.
- **Full series list** showing status, latest release, and next known book.
- **Mark a series as ended** — Audible doesn't publish this anywhere, so it's
  a manual toggle.
- **Daily background refresh** of every subscribed series.
- **Dark mode by default**, with an automatic light-mode fallback based on
  your system preference.
- Single container, SQLite storage, no external services or API keys required.

## Screenshots

### Dashboard (dark mode)
![Dashboard in dark mode](docs/screenshots/dashboard.webp)

### Dashboard (light mode)
![Dashboard in light mode](docs/screenshots/dashboard-light.webp)

### Search
![Series search page](docs/screenshots/search.webp)

## How it works

- **FastAPI** backend serving server-rendered Jinja2 templates (no JS build
  step, no frontend framework).
- **SQLite** for storage via SQLAlchemy.
- **APScheduler** runs an in-process daily job that re-scrapes every
  subscribed series and updates release dates, new books, and series names.
- Series discovery and lookup both scrape Audible's public HTML pages
  directly — `audible.com/series/...` for a series' full book list, and
  `audible.com/search?keywords=...` for the search page.

## Installation

### Requirements

- Docker and Docker Compose

### Quick start

```bash
git clone https://github.com/ufondu88/audiobook-series-tracker.git
cd audiobook-series-tracker
docker compose up -d --build
```

The app will be available at **http://localhost:8241**. Visit it and sign up
for the first account — this is the only time `/signup` is reachable. It
becomes the admin account, automatically takes ownership of any series
already in the database (relevant if you're migrating from an older
single-user version of this app), and can create further users from
`/admin/users` afterward.

Subscribed series and release data live in `./data/audiobooks.db` (SQLite),
which is mounted as a volume so it persists across container rebuilds. A
session-signing secret is generated on first run and stored alongside it at
`./data/.session_secret` — back both up together if you care about staying
logged in across reinstalls.

### Running without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Notes and limitations

- This relies on scraping Audible's public pages rather than an official API
  (none exists for this use case — see the note in `app/scraper.py`). If
  Audible changes their page layout, scraping may break until it's updated.
- "Series ended" is a manual flag you set yourself; there's no reliable public
  signal for this on Audible.
- Only the very first account can self-register; every account after that is
  created by an admin. That's a deliberate choice so this can be exposed to
  family/roommates without open signup, but it also means there's no
  self-service password reset — an admin has to delete and recreate an
  account if someone forgets their password.
