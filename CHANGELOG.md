# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`, where
MAJOR is a breaking change, MINOR is a new backward-compatible feature, and
PATCH is a fix with no new capability.

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
