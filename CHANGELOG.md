# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`, where
MAJOR is a breaking change, MINOR is a new backward-compatible feature, and
PATCH is a fix with no new capability.

## [1.4.3] - 2026-08-12

- Fixed spurious "failed check" warnings on the scraper health page for
  series that were actually fine. The daily background refresh hit Audible
  for every subscribed series back-to-back with no delay between requests,
  which is exactly the pattern Audible's rate limiter has always rejected
  elsewhere in this app (the bulk-import flow already sleeps between
  requests for the same reason) — a request that landed mid-rate-limit got
  an empty response and was misreported as "page layout may have changed."
  The daily refresh now paces requests the same way import already does.

## [1.4.2] - 2026-08-11

- Fixed backwards redirects on the per-series Watchlist view: acknowledging
  a single book from a filtered series now stays on that series (so you can
  keep clicking through it) instead of bouncing to the full mixed
  watchlist; "Acknowledge all" from a filtered series now goes to the full
  watchlist instead of redrawing the series view it just emptied.

## [1.4.1] - 2026-08-10

- Fixed duplicate books showing up (most visibly on the Watchlist): Audible's
  series pages sometimes list the same book twice under different ASINs (a
  second edition/format that isn't inside the numbered "Book N" listing).
  The scraper now keeps one entry per title per series, preferring the
  positioned (numbered) listing. Also cleaned up 81 existing duplicate rows
  already in the database — any per-book status (in-library, acknowledged,
  download requests) on either duplicate was merged, not lost.

## [1.4.0] - 2026-08-10

- Click a series name on the Watchlist to view just that series, and
  "Acknowledge all" now scopes to whatever you're currently viewing — so
  clearing a big backlog for one long-running series no longer requires
  wading through (or clicking through one-by-one) every other series in
  your subscriptions.

## [1.3.0] - 2026-08-09

- The Watchlist table's columns (Book, Series, Released) are now sortable —
  click a header to sort ascending, click again for descending.

## [1.2.0] - 2026-08-09

- Added a **Watchlist** (📋 in the top bar): a persistent, unbounded list of
  released books from your subscriptions you haven't dealt with yet. Unlike
  the dashboard's "recently released" section, nothing falls off this list
  just because time passed — a book released 6 months ago that you never
  logged in to see still shows up. Acknowledge one book, or clear the whole
  list at once. Muted series are excluded, matching how muting already
  behaves everywhere else in the app.
- Fixed: disconnecting Audiobookshelf no longer wipes your acknowledgment
  history — it now only clears the Audiobookshelf-specific fields instead of
  deleting the whole per-book status row.

## [1.1.0] - 2026-08-09

- Connect your own Audiobookshelf instance (per-user, in Profile → Integrations)
  to see which recently-released books you already have — a background job
  rechecks your library every 6 hours, and dashboard cards get an "In library"
  badge once confirmed.
- Connect your own Prowlarr instance (same Integrations page) to get a
  "Download" button on books you don't have yet — searches your configured
  indexers (filtered to the audiobook category) and lets you grab a release,
  which hands off to your download client. No import/organization is done by
  this app; that's on Prowlarr's downstream pipeline.
- Both are entirely optional — a user with neither connected sees no change
  to the dashboard.

## [1.0.0] - 2026-08-05

First tagged release. Everything built up to this point, treated as the
initial stable baseline since the app has been in real daily use throughout:

- Subscribe to Audible audiobook series by searching directly in the app
  (or pasting a URL/ASIN), with a dashboard showing "recently released" and
  "releasing soon" windows plus a full series list with status and next book.
- Multi-user accounts with an admin role: the first account created becomes
  admin and can create/manage other users from `/admin/users`; public signup
  closes after that first account.
- Installable as a PWA (Android/iOS/desktop) with Web Push notifications —
  new book announced, release date confirmed, or a book's release day
  arrives — each including the book's cover art as the notification icon.
- A top bar with a unified search box (live-filters your subscriptions,
  or searches Audible on enter), a Settings menu (dark/light/auto theme,
  notification toggle), and a Profile menu (change password, admin links,
  log out). Responsive down to phone-sized screens.
- Bulk import: paste a list of titles, review the matches, subscribe to
  what's confirmed.
- Mute a series without unsubscribing; a weekly digest option as an
  alternative to per-book push notifications; a personal iCal feed of
  upcoming releases; CSV export of your subscriptions.
- An admin-facing scraper health page tracking series that have started
  failing to update.
- Scraping is done via `curl` (shelled out directly) rather than a Python
  HTTP client, since Audible's WAF blocks the latter outright regardless of
  TLS fingerprint impersonation.
