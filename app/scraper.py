# Audible has no public API for series/release data. Audnexus (api.audnex.us)
# was evaluated first but only supports lookup-by-known-ASIN, not series
# listing or search. So this scrapes Audible's own public series and search
# pages instead, which robots.txt explicitly permits crawling.

import datetime
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ASIN_RE = re.compile(r"/([A-Z0-9]{10})(?:[/?]|$)")


class SeriesPageError(Exception):
    pass


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
    return datetime.date(2000 + year, month, day)


def _parse_position(item) -> float | None:
    heading = item.find("h2")
    if not heading:
        return None
    match = re.search(r"Book\s+([\d.]+)", heading.get_text(strip=True))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_series_page(html: str, fallback_url: str) -> ScrapedSeries:
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else "Unknown series"

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

    if not books:
        raise SeriesPageError("No books found on series page — page layout may have changed.")

    series_asin_match = ASIN_RE.search(fallback_url)
    series_asin = series_asin_match.group(1) if series_asin_match else fallback_url

    return ScrapedSeries(asin=series_asin, name=name, url=fallback_url, books=books)


def fetch_series(url_or_asin: str) -> ScrapedSeries:
    asin = extract_series_asin(url_or_asin)
    url = url_or_asin if url_or_asin.startswith("http") else _series_url(asin)

    response = httpx.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=20.0)
    response.raise_for_status()

    return parse_series_page(response.text, fallback_url=url)


def _name_from_slug(slug: str) -> str:
    words = slug.split("-")
    if words and words[-1].lower() == "audiobooks":
        words = words[:-1]
    return " ".join(words)


def search_series(query: str) -> list[SeriesSearchResult]:
    response = httpx.get(
        "https://www.audible.com/search",
        params={"keywords": query},
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=20.0,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

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
