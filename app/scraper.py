# Audible has no public API for series/release data. Audnexus (api.audnex.us)
# was evaluated first but only supports lookup-by-known-ASIN, not series
# listing or search. So this scrapes Audible's own public series and search
# pages instead, which robots.txt explicitly permits crawling.

import datetime
import json
import logging
import re
import subprocess
import urllib.parse
from dataclasses import dataclass

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ASIN_RE = re.compile(r"/([A-Z0-9]{10})(?:[/?]|$)")

_STATUS_MARKER = "__HTTP_STATUS__"


class SeriesPageError(Exception):
    pass


def _curl_get(url: str, params: dict[str, str] | None = None) -> str:
    # Audible's WAF blocks plain httpx requests (confirmed: identical requests via
    # curl succeed, via httpx/curl_cffi with full Chrome TLS impersonation still get
    # a 503 — this isn't a simple TLS/JA3 fingerprint thing). Shelling out to the
    # actual curl binary reliably works, so that's what both scraping paths use.
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    try:
        result = subprocess.run(
            [
                "curl", "-sL",
                "-A", USER_AGENT,
                "--max-time", "25",
                "-w", f"\n{_STATUS_MARKER}%{{http_code}}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise SeriesPageError(f"Request to Audible timed out: {url}") from exc

    if result.returncode != 0:
        raise SeriesPageError(f"curl failed ({result.returncode}) fetching {url}: {result.stderr.strip()}")

    body, _, status = result.stdout.rpartition(_STATUS_MARKER)
    if not status.strip().startswith("2"):
        raise SeriesPageError(f"Audible returned HTTP {status.strip()} for {url}")

    return body


@dataclass
class ScrapedBook:
    asin: str
    title: str
    position: float | None
    release_date: datetime.date | None
    url: str
    image_url: str | None


@dataclass
class ScrapedSeries:
    asin: str
    name: str
    url: str
    books: list[ScrapedBook]


@dataclass
class SeriesSearchResult:
    asin: str
    name: str
    url: str
    author: str | None
    sample_title: str


def extract_series_asin(url_or_asin: str) -> str:
    candidate = url_or_asin.strip()
    if re.fullmatch(r"[A-Z0-9]{10}", candidate):
        return candidate
    match = re.search(r"/series/[^/]+/([A-Z0-9]{10})", candidate)
    if match:
        return match.group(1)
    raise SeriesPageError(f"Could not find a series ASIN in: {url_or_asin!r}")


def _series_url(asin: str) -> str:
    return f"https://www.audible.com/series/x/{asin}"


def _parse_release_date(text: str) -> datetime.date | None:
    match = re.search(r"(\d{2})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    month, day, year = (int(part) for part in match.groups())
    try:
        return datetime.date(2000 + year, month, day)
    except ValueError:
        # A single book with an unparseable date shouldn't abort adding the
        # whole series — confirmed via a real Audible page whose markup no
        # longer matches this format for some titles (see _parse_new_template).
        logger.warning("Unparseable release date text %r, treating as unknown", text)
        return None


def _parse_new_template_release_date(script_tag) -> datetime.date | None:
    if not script_tag or not script_tag.string:
        return None
    try:
        data = json.loads(script_tag.string)
        return datetime.date.fromisoformat(data["releaseDate"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _extract_book_number(text: str) -> float | None:
    match = re.search(r"Book\s+([\d.]+)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_position(item) -> float | None:
    heading = item.find("h2")
    if not heading:
        return None
    return _extract_book_number(heading.get_text(strip=True))


def _dedupe_by_title(books: list[ScrapedBook]) -> list[ScrapedBook]:
    """Audible's series pages sometimes list the same book twice under different
    ASINs — a second edition/format callout that isn't inside the "Book N"
    heading the position parser looks for, so it comes through with
    position=None alongside the real, positioned entry. Keep one entry per
    title: prefer whichever has a position (the one actually placed in the
    numbered list), otherwise keep the first one encountered (stable across
    re-scrapes since page DOM order doesn't change)."""
    best_by_title: dict[str, ScrapedBook] = {}
    for book in books:
        existing = best_by_title.get(book.title)
        if existing is None or (existing.position is None and book.position is not None):
            best_by_title[book.title] = book

    deduped: list[ScrapedBook] = []
    seen_titles: set[str] = set()
    for book in books:
        if book.title in seen_titles:
            continue
        seen_titles.add(book.title)
        deduped.append(best_by_title[book.title])
    return deduped


def _parse_old_template(soup: BeautifulSoup, fallback_url: str) -> list[ScrapedBook]:
    books: list[ScrapedBook] = []
    for item in soup.select("li.productListItem"):
        item_id = item.get("id", "")
        asin_match = re.search(r"product-list-item-([A-Z0-9]{10})", item_id)
        if not asin_match:
            continue
        asin = asin_match.group(1)

        title = item.get("aria-label", "").strip() or asin

        link = item.find("a", href=re.compile(r"/pd/"))
        url = f"https://www.audible.com{link['href'].split('?')[0]}" if link else fallback_url

        release_date = None
        date_item = item.select_one("li.releaseDateLabel span")
        if date_item:
            release_date = _parse_release_date(date_item.get_text(strip=True))

        position = _parse_position(item)

        image_url = None
        img = item.select_one("div.adbl-asin-impression img")
        if img and img.get("src"):
            image_url = re.sub(r"_SL\d+_", "_SL120_", img["src"])

        books.append(
            ScrapedBook(
                asin=asin,
                title=title,
                position=position,
                release_date=release_date,
                url=url,
                image_url=image_url,
            )
        )
    return books


def _parse_new_template(soup: BeautifulSoup) -> list[ScrapedBook]:
    # Audible has been rolling out a redesigned series page (built on
    # <adbl-product-row> web components) that carries no
    # li.productListItem markup at all — confirmed against ~50% of a
    # sample of 106 real series pages. Each row embeds a small JSON blob
    # with a clean, unambiguous ISO release date, which is also more
    # reliable than the old template's hand-scraped "MM-DD-YY" text.
    books: list[ScrapedBook] = []
    for row in soup.select("adbl-product-row"):
        link = row.select_one('a[href*="/pd/"]')
        if not link or not link.get("href"):
            continue
        asin_match = ASIN_RE.search(link["href"])
        if not asin_match:
            continue
        asin = asin_match.group(1)

        title_el = row.select_one('h3[slot="title"]')
        title = title_el.get_text(strip=True) if title_el else asin

        url = f"https://www.audible.com{link['href'].split('?')[0]}"

        release_date = _parse_new_template_release_date(
            row.find("script", {"type": "application/json"})
        )

        position = _extract_book_number(row.get("series-header", ""))

        image_url = None
        img = row.select_one("img")
        if img and img.get("src"):
            image_url = re.sub(r"_SL\d+_", "_SL120_", img["src"])

        books.append(
            ScrapedBook(
                asin=asin,
                title=title,
                position=position,
                release_date=release_date,
                url=url,
                image_url=image_url,
            )
        )
    return books


def parse_series_page(html: str, fallback_url: str) -> ScrapedSeries:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else "Unknown series"

    books = _parse_old_template(soup, fallback_url)
    if not books:
        books = _parse_new_template(soup)

    if not books:
        raise SeriesPageError("No books found on series page — page layout may have changed.")

    books = _dedupe_by_title(books)

    series_asin_match = ASIN_RE.search(fallback_url)
    series_asin = series_asin_match.group(1) if series_asin_match else fallback_url

    return ScrapedSeries(asin=series_asin, name=name, url=fallback_url, books=books)


def fetch_series(url_or_asin: str) -> ScrapedSeries:
    asin = extract_series_asin(url_or_asin)
    url = url_or_asin if url_or_asin.startswith("http") else _series_url(asin)

    html = _curl_get(url)

    return parse_series_page(html, fallback_url=url)


def _name_from_slug(slug: str) -> str:
    words = slug.split("-")
    if words and words[-1].lower() == "audiobooks":
        words = words[:-1]
    return " ".join(words)


FOREIGN_EDITION_HINTS = (
    "french", "german", "spanish", "italian", "portuguese", "japanese",
    "korean", "chinese", "russian", "polish", "dutch", "edition",
)


def _normalize_for_match(name: str) -> str:
    name = name.lower().replace("'", "")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def find_best_match(target_name: str, results: list[SeriesSearchResult]) -> SeriesSearchResult | None:
    """Picks the most likely match for a plain-text series/book name out of a set of
    search results. Returns None when nothing matches confidently or multiple
    equally-ranked candidates tie (ambiguous), rather than guessing wrong."""
    target_norm = _normalize_for_match(target_name)
    candidates = []
    for result in results:
        result_norm = _normalize_for_match(result.name)
        is_foreign = any(hint in result_norm for hint in FOREIGN_EDITION_HINTS)
        target_is_foreign = any(hint in target_norm for hint in FOREIGN_EDITION_HINTS)
        if is_foreign and not target_is_foreign:
            continue
        if result_norm == target_norm:
            candidates.append((0, result))
        elif target_norm in result_norm or result_norm in target_norm:
            candidates.append((1, result))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: (pair[0], len(pair[1].name)))
    best_rank = candidates[0][0]
    tied = [result for rank, result in candidates if rank == best_rank]
    return tied[0] if len(tied) == 1 else None


def search_series(query: str) -> list[SeriesSearchResult]:
    html = _curl_get("https://www.audible.com/search", params={"keywords": query})
    soup = BeautifulSoup(html, "html.parser")

    results: dict[str, SeriesSearchResult] = {}
    for item in soup.select("li.productListItem"):
        series_link = item.select_one('a[href*="/series/"]')
        if series_link is None:
            continue
        href = series_link["href"].split("?")[0]
        match = re.search(r"/series/([^/]+)/([A-Z0-9]{10})", href)
        if not match:
            continue
        slug, series_asin = match.groups()
        if series_asin in results:
            continue

        author_link = item.select_one('a[href*="/author/"]')
        results[series_asin] = SeriesSearchResult(
            asin=series_asin,
            name=_name_from_slug(slug),
            url=f"https://www.audible.com{href}",
            author=author_link.get_text(strip=True) if author_link else None,
            sample_title=item.get("aria-label", "").strip(),
        )

    return list(results.values())
