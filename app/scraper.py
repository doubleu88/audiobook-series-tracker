# Audible's Catalog API (/1.0/catalog/products) provides structured series
# relationships and child book metadata. Series data is fetched directly via
# the Catalog API, with multi-market deduplication favoring canonical US editions
# and proper handling for upcoming/Date TBD placeholder releases.
# Plain-text series search uses Audible's search page via curl.

import datetime
import logging
import re
import subprocess
import urllib.parse
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
import httpx

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
class ScrapedEdition:
    asin: str
    title: str
    sku: str | None = None
    format_type: str | None = None
    is_primary: bool = False


@dataclass
class ScrapedBook:
    asin: str
    title: str
    position: float | None
    release_date: datetime.date | None
    url: str
    image_url: str | None
    editions: list[ScrapedEdition] = field(default_factory=list)


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
    book_count: int | None = None


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


def _is_us_edition(prod: dict) -> bool:
    # 1. Direct Audible Catalog API rights check
    if prod.get("is_world_rights") is True:
        return True
    regions = prod.get("distribution_rights_region")
    if regions is not None:
        return "US" in regions

    # 2. Heuristic fallback based on SKU and publisher
    sku = prod.get("sku") or ""
    pub = (prod.get("publisher_name") or "").lower()
    if any(sku.endswith(m) for m in ("UK", "AU", "CA", "DE", "FR")):
        return False
    if re.search(r"_[A-Z]{2,3}(UK|AU|CA|DE|FR)_", sku):
        return False
    if any(fp in sku for fp in ("_HODD_", "_HBGA_", "_WFHO_", "_BLND_", "_ORIO_", "_QUER_", "_HOWE_")):
        return False
    if any(fp in pub for fp in (
        "hodder", "bolinda", "w.f. howes", "wf howes", "howes", "orion", "quercus",
        "little, brown audio", "little, brown book group", "time warner", "pan macmillan",
    )):
        return False
    return True


def _classify_edition(prod: dict) -> str:
    title = (prod.get("title") or "").lower()
    subtitle = (prod.get("subtitle") or "").lower()
    full_text = f"{title} {subtitle}"
    if any(term in full_text for term in ("dramatized", "graphicaudio", "graphic audio", "soundtrack")):
        return "dramatized"
    if "booktrack" in full_text:
        return "booktrack"
    if any(term in full_text for term in ("box set", "boxed set", "omnibus", "collection")) or re.search(r"books?\s*\d+\s*[-–—to]+\s*\d+", full_text):
        return "box_set"
    if "abridged" in full_text and "unabridged" not in full_text:
        return "abridged"
    if not _is_us_edition(prod):
        return "foreign"
    return "standard"


def _is_specialty_edition(prod: dict) -> bool:
    fmt = _classify_edition(prod)
    return fmt in ("booktrack", "dramatized", "abridged", "box_set")


def _parse_omnibus_range(*texts: str | None) -> tuple[float, float] | None:
    for text in texts:
        if not text:
            continue
        s = str(text).strip()
        # 1. Direct sequence range like '1-3', '1 - 3', '1 to 3'
        m1 = re.match(r"^(\d+(?:\.\d+)?)\s*(?:[-–—]|\bto\b)\s*(\d+(?:\.\d+)?)$", s, re.IGNORECASE)
        if m1:
            try:
                return float(m1.group(1)), float(m1.group(2))
            except ValueError:
                pass
        # 2. 'books 1-3' or 'books 1 to 3'
        m2 = re.search(r"books?\s*(\d+(?:\.\d+)?)\s*(?:[-–—]|\bto\b)\s*(\d+(?:\.\d+)?)", s, re.IGNORECASE)
        if m2:
            try:
                return float(m2.group(1)), float(m2.group(2))
            except ValueError:
                pass
        # 3. 'Book 1, 2, 3' or 'Books 1, 2 and 3'
        m3 = re.search(r"books?\s*(\d+)(?:\s*,\s*\d+)*\s*(?:,|and|\s)+\s*(\d+)", s, re.IGNORECASE)
        if m3:
            try:
                return float(m3.group(1)), float(m3.group(2))
            except ValueError:
                pass
    return None


def _parse_sequence(seq_str: str | None) -> float | None:
    if not seq_str:
        return None
    s = str(seq_str).strip()
    if any(sep in s for sep in ("-", "–", "—", " to ", ",")):
        return None
    try:
        return float(s)
    except ValueError:
        pass
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _norm_title_for_group(title: str, series_name: str = "") -> str:
    # 1. Strip bracketed / parenthetical expressions (e.g. (Dramatized Adaptation), [Booktrack Edition])
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", "", title)
    # 2. Strip leading series name prefix (e.g. "Warlock Holmes: The Finality Problem" -> "The Finality Problem")
    if series_name:
        s_pat = re.escape(series_name.strip()) + r"[:\s—\-]+"
        t = re.sub(f"^{s_pat}", "", t, flags=re.IGNORECASE)
    # 3. Strip subtitles after colon, em-dash, or hyphen-space
    t = t.split(":")[0].split("—")[0].split(" - ")[0].strip().lower()
    cleaned = re.sub(r"[^a-z0-9]", "", t)
    return cleaned or re.sub(r"[^a-z0-9]", "", title.lower())


def fetch_series_via_api(series_asin: str, fallback_url: str) -> ScrapedSeries:
    url = f"https://api.audible.com/1.0/catalog/products/{series_asin}"
    params = {"response_groups": "product_attrs,product_desc,relationships"}
    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params, headers=headers)
            if resp.status_code != 200:
                raise SeriesPageError(f"Audible API returned HTTP {resp.status_code} for series {series_asin}")
            data = resp.json().get("product", {})

            series_name = data.get("title") or "Unknown series"
            relationships = data.get("relationships", []) or []
            series_rels = [
                r for r in relationships
                if r.get("relationship_type") == "series" and r.get("asin")
            ]
            if not series_rels:
                raise SeriesPageError(f"No series relationships found in Audible API for series {series_asin}")

            child_asins = list(dict.fromkeys(r["asin"] for r in series_rels))
            products_by_asin: dict[str, dict] = {}
            for i in range(0, len(child_asins), 50):
                chunk = child_asins[i : i + 50]
                try:
                    p_resp = client.get(
                        "https://api.audible.com/1.0/catalog/products",
                        params={
                            "asins": ",".join(chunk),
                            "response_groups": "product_attrs,product_desc,contributors,media,sku,rights",
                        },
                        headers=headers,
                    )
                    if p_resp.status_code == 200:
                        for p in p_resp.json().get("products", []):
                            products_by_asin[p["asin"]] = p
                except Exception as chunk_exc:
                    logger.warning("Error fetching product chunk for series %s: %s", series_asin, chunk_exc)

        # Group candidate child products into series slots.
        # A slot represents a single book entry / position in the series, which can hold
        # multiple editions (e.g. US edition, UK edition, GraphicAudio, Booktrack).
        slots: dict[tuple, list[tuple[dict, dict]]] = {}
        for r_item in series_rels:
            asin = r_item["asin"]
            p = products_by_asin.get(asin, {})
            pos = _parse_sequence(r_item.get("sequence"))
            title = p.get("title") or asin
            norm = _norm_title_for_group(title, series_name)

            if pos is not None:
                slot_key = ("pos", pos)
            else:
                slot_key = ("title", norm)

            slots.setdefault(slot_key, []).append((r_item, p))

        # Check for omnibus / box sets that span multiple positions and attach to covered slots
        omnibus_asins_absorbed = set()
        for r_item in series_rels:
            asin = r_item["asin"]
            p = products_by_asin.get(asin, {})
            rng = _parse_omnibus_range(r_item.get("sequence"), p.get("title"), p.get("subtitle"))
            if rng:
                start, end = rng
                covered_slots = [
                    slot_key for slot_key in slots.keys()
                    if slot_key[0] == "pos" and start <= slot_key[1] <= end
                ]
                if covered_slots:
                    omnibus_asins_absorbed.add(asin)
                    for slot_key in covered_slots:
                        items = slots[slot_key]
                        if not any(it_r["asin"] == asin for it_r, _ in items):
                            items.append((r_item, p))

        # Remove standalone unnumbered slots that were just absorbed omnibuses
        if omnibus_asins_absorbed:
            to_delete = [
                slot_key for slot_key, items in slots.items()
                if slot_key[0] == "title" and len(items) == 1 and items[0][0]["asin"] in omnibus_asins_absorbed
            ]
            for k in to_delete:
                del slots[k]

        books: list[ScrapedBook] = []
        for slot_key, group in slots.items():
            def _candidate_rank(item):
                rel, prod = item
                sku = prod.get("sku") or rel.get("sku") or ""
                is_placeholder = sku.startswith("PL_HLDR") or prod.get("release_date") == "2200-01-01"
                f = _classify_edition(prod)
                return (
                    is_placeholder,
                    not _is_us_edition(prod),
                    f == "box_set",
                    f in ("dramatized", "booktrack", "abridged"),
                    rel["asin"],
                )

            group.sort(key=_candidate_rank)
            best_rel, best_prod = group[0]
            asin = best_rel["asin"]
            title = best_prod.get("title") or asin
            position = slot_key[1] if slot_key[0] == "pos" else _parse_sequence(best_rel.get("sequence"))
            sku = best_prod.get("sku") or ""
            raw_date = best_prod.get("release_date")

            if raw_date == "2200-01-01" or sku.startswith("PL_HLDR"):
                release_date = None
                book_url = fallback_url
            elif raw_date:
                try:
                    release_date = datetime.date.fromisoformat(raw_date)
                except ValueError:
                    release_date = None
                book_url = f"https://www.audible.com/pd/{asin}"
            else:
                release_date = None
                book_url = f"https://www.audible.com/pd/{asin}"

            images = best_prod.get("product_images", {}) or {}
            image_url = images.get("500") or images.get("120")

            editions = []
            seen_edition_asins = set()
            for idx, (item_rel, item_prod) in enumerate(group):
                ed_asin = item_rel["asin"]
                if ed_asin not in seen_edition_asins:
                    seen_edition_asins.add(ed_asin)
                    editions.append(
                        ScrapedEdition(
                            asin=ed_asin,
                            title=item_prod.get("title") or ed_asin,
                            sku=item_prod.get("sku"),
                            format_type=_classify_edition(item_prod),
                            is_primary=(idx == 0),
                        )
                    )

            books.append(
                ScrapedBook(
                    asin=asin,
                    title=title,
                    position=position,
                    release_date=release_date,
                    url=book_url,
                    image_url=image_url,
                    editions=editions,
                )
            )

        books.sort(
            key=lambda b: (
                b.position if b.position is not None else 9999,
                b.release_date or datetime.date.max,
                b.title,
            )
        )
        if not books:
            raise SeriesPageError(f"No books found in Audible API for series {series_asin}")

        return ScrapedSeries(
            asin=series_asin, name=series_name, url=fallback_url, books=books
        )
    except SeriesPageError:
        raise
    except Exception as e:
        logger.warning("Audible API series lookup failed for %s: %s", series_asin, e)
        raise SeriesPageError(f"Audible API series lookup failed for {series_asin}: {e}") from e


def fetch_series(url_or_asin: str) -> ScrapedSeries:
    asin = extract_series_asin(url_or_asin)
    url = url_or_asin if url_or_asin.startswith("http") else _series_url(asin)
    return fetch_series_via_api(asin, fallback_url=url)


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

        author = None
        author_item = item.select_one("li.authorLabel")
        if author_item:
            author_text = author_item.get_text(" ", strip=True)
            author = re.sub(r"^By:\s*", "", author_text)
            author = re.sub(r"\s*,\s*", ", ", author).strip()
        elif item.select_one('a[href*="/author/"]'):
            author = item.select_one('a[href*="/author/"]').get_text(strip=True)

        results[series_asin] = SeriesSearchResult(
            asin=series_asin,
            name=_name_from_slug(slug),
            url=f"https://www.audible.com{href}",
            author=author,
            sample_title=item.get("aria-label", "").strip(),
        )

    if results:
        asins_str = ",".join(results.keys())
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(
                    "https://api.audible.com/1.0/catalog/products",
                    params={
                        "asins": asins_str,
                        "response_groups": "relationships,product_desc,contributors",
                    },
                    headers={"User-Agent": USER_AGENT},
                )
                if resp.status_code == 200:
                    for p in resp.json().get("products", []):
                        p_asin = p.get("asin")
                        if p_asin in results:
                            if p.get("title"):
                                results[p_asin].name = p.get("title")
                            rels = [
                                r
                                for r in p.get("relationships", [])
                                if r.get("relationship_type") == "series"
                            ]
                            if rels:
                                results[p_asin].book_count = len(rels)
                            authors = [
                                a.get("name")
                                for a in p.get("authors", [])
                                if a.get("name")
                            ]
                            if authors:
                                results[p_asin].author = ", ".join(authors)
        except Exception as exc:
            logger.warning(
                "Failed to prefetch series metadata for search results: %s",
                exc,
            )

    return list(results.values())
