# Changelog

All notable changes to this project are documented here. Versioning follows
[Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`, where
MAJOR is a breaking change, MINOR is a new backward-compatible feature, and
PATCH is a fix with no new capability.

## [1.13.1] - 2026-08-20

- Fixed the changelog-on-merge automation failing with a 403 on PRs
  from external contributors' forks (GitHub forces GITHUB_TOKEN to
  read-only for pull_request-triggered workflows on fork PRs, no
  matter what permissions are declared) by switching to
  pull_request_target, which is safe here since no fork code is ever
  checked out or executed. Also reconciles the CHANGELOG.md entry PR
  #12's failed run never wrote.

## [1.13.0] - 2026-08-20

Augment existing search feature for the Watchlist page to also search by
book titles as well as series. This leaves the Dashboard search function
unchanged (and so looking only for series names), on the grounds that the
Dashboard reliably displays all of the series, while the Watchlist
reliably displays everything. Contributed by @wtanksleyjr (#12).

## [1.12.0] - 2026-08-20

- Added timestamped, properly-configured logging across the whole app
  (previously there was no logging setup at all, so most errors were
  never actually visible in the logs). Every route and background job
  now logs failures with enough context to diagnose them, background
  jobs isolate per-item failures so one bad series/user can't silently
  abort the rest of a batch, and a genuinely unexpected error is always
  captured with a full traceback instead of possibly vanishing. Admins
  can also flip on verbose debug logging at runtime from
  `/admin/health`, no restart required, for tracking down issues that
  need more detail than the normal logs show.

## [1.11.1] - 2026-08-19

- Fixed series lookups silently failing for a large share of series
  (roughly half, in a live test of 106 real ones) because Audible has
  rolled out a redesigned series page the scraper didn't recognize at
  all — it's now parsed alongside the original layout. Also fixed a
  single book with a malformed release date aborting the entire
  add-series request instead of just leaving that book's date unknown.

## [1.11.0] - 2026-08-19

- The profile menu now shows the app's current version, and flags when
  a newer release is available on GitHub (checked periodically,
  cached), linking straight to it. Falls back to just showing the
  current version if the check can't reach GitHub.

## [1.10.4] - 2026-08-18

- Fixed the self-certified "check" status failing with a 422 because
  GitHub doesn't recognize a commit that hasn't been pushed anywhere
  yet. The commit is now pushed to a scratch branch first so the
  status can be attached to it, then fast-forwarded onto main. Also
  reconciles the CHANGELOG.md entry PR #8's own failed run never wrote.

## [1.10.3] - 2026-08-18

- Fixed changelog-on-merge.yml's direct push to main being rejected
  (GH006) now that main requires the "check" status on every push, not
  just PR merges. The bot's commit now self-certifies a passing "check"
  status for its own SHA before pushing, since it's a deterministic
  derivative of a PR that already passed the real check. Also
  reconciles the CHANGELOG.md entry PR #7's failed run never wrote.

## [1.10.2] - 2026-08-18

- Final validation pass confirming the changelog/release automation's
  direct-to-main pushes still succeed now that branch protection requires
  the PR checklist status check.

## [1.10.1] - 2026-08-18

- Documented in CONTRIBUTING.md how changelog-on-merge.yml hands off to
  release.yml via workflow_dispatch, and this PR is also a real end-to-end
  test of the workflow_dispatch fix for that handoff.

## [1.10.0] - 2026-08-18

- Linked CONTRIBUTING.md from the README so the new PR process is
  discoverable, and this is also a live end-to-end test of the new
  PR-template-driven changelog/release automation itself.

## [1.9.0] - 2026-08-18

- Added a required PR template: every pull request must now check exactly
  one version-bump type (Major/Minor/Patch) and fill in a changelog
  description, enforced by a required status check that blocks merging
  until both are present. On merge, that description and bump type are
  used to automatically write the CHANGELOG.md entry (no more manually
  editing it before merging), which in turn triggers the existing
  auto-tag-and-release workflow. See CONTRIBUTING.md.

## [1.8.2] - 2026-08-18

- Merges to main now automatically get tagged and released on GitHub — a
  new workflow checks CHANGELOG.md's top entry after every push, and if
  its version doesn't have a matching tag yet, creates the tag and a
  GitHub Release using that entry as the notes. Replaces doing this by
  hand after every merge.

## [1.8.1] - 2026-08-18

- The dashboard's and Watchlist's "Hide acknowledged" toggles now remember
  their preferences independently — toggling one no longer silently changes
  what the other page shows.

## [1.8.0] - 2026-08-18

- Added a "Hide acknowledged" toggle to the Watchlist, matching the dashboard's
  toggle behavior. On by default, remembered across visits, and instant without
  a page reload.
- When acknowledged books are displayed on the Watchlist, they are visually
  dimmed and feature a "Watch" action link to easily undo acknowledgment and
  return them to your active watched list.
- Updated the Watchlist header counter to display `(watched / total)`, e.g.
  `(0 / 5)`, showing both your pending backlog and total released books.
  Contributed by @wtanksleyjr (#4).

## [1.7.0] - 2026-08-18

- Added a "#watching" column to the "All series" table showing
  (unacknowledged / total released) for each series, linking straight to
  that series' filtered Watchlist. Contributed by @wtanksleyjr (#3).

## [1.6.2] - 2026-08-18

- Simplified the 1.6.1 fix: dropped the browser-cookie mechanism and just
  trust the server's clock, now that its timezone is actually configured
  correctly. The cookie approach only ever fixed the dashboard — push
  notifications and the weekly digest have no browser to read a cookie
  from, so they were still exposed to the same class of bug. A correctly
  configured server clock fixes date classification everywhere at once,
  with no added client-side complexity.

## [1.6.1] - 2026-08-18

- Fixed "recently released" showing books that hadn't actually released
  yet from the user's point of view. Root cause was two-fold: the
  container's clock was silently running on UTC despite being configured
  for a specific timezone (fixed — the base image had no timezone data
  installed at all, so the TZ setting was a no-op), and even with the
  server's clock correct, a user in a different timezone than the server
  would still see the wrong day. The dashboard's "recently released" vs
  "releasing soon" split now uses the browser's own local date (sent via a
  cookie set on page load) instead of the server's clock.

## [1.6.0] - 2026-08-14

- Added a "Hide acknowledged" toggle to the Recently released section —
  the checkbox @wtanksleyjr wondered about in #2. On by default (matching
  1.5.0's behavior), remembered across visits, and instant (no page
  reload). Combines correctly with the top-bar series search — searching
  and hiding acknowledged books both apply at once rather than one
  overriding the other.

## [1.5.0] - 2026-08-14

- The "Recently released" dashboard section now hides books you've already
  acknowledged (via the Watchlist), so it doesn't stay cluttered with
  things you've already dealt with. Contributed by @wtanksleyjr (#2).
- Internal: that filter now does one bulk query for the user's acknowledged
  books instead of one query per book, reusing the same pattern already
  used elsewhere in this route.

## [1.4.4] - 2026-08-12

- The 1.4.3 fix (pacing requests during the daily refresh) helped but
  didn't fully solve the false "failed check" problem — further testing
  showed Audible's WAF fails intermittently in a way that isn't purely
  rate-limit/timing-shaped (the same series can fail consistently while
  others succeed at an identical delay). `refresh_series` now retries a
  failed fetch up to 2 more times (3s apart) before actually recording it
  as a failure — verified this clears every series that was previously
  misreported as broken.

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
