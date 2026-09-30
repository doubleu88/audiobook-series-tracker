import datetime
import json
import subprocess
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app import scraper
from app.scraper import (
    ScrapedBook,
    ScrapedSeries,
    SeriesPageError,
    SeriesSearchResult,
    _curl_get,
    _dedupe_by_title,
    _extract_book_number,
    _name_from_slug,
    _normalize_for_match,
    _parse_new_template,
    _parse_new_template_release_date,
    _parse_old_template,
    _parse_position,
    _parse_release_date,
    _series_url,
    extract_series_asin,
    fetch_series,
    find_best_match,
    parse_series_page,
    search_series,
)

SERIES_URL = "https://www.audible.com/series/Test-Series/B0SERIES01"


def soup_of(html):
    return BeautifulSoup(html, "html.parser")


def book(title="T", position=None, asin="B000000001"):
    return ScrapedBook(asin=asin, title=title, position=position, release_date=None,
                       url="u", image_url=None)


def sr(name, asin="B000000001"):
    return SeriesSearchResult(asin=asin, name=name, url="u", author=None, sample_title="s")


OLD_HTML = """
<html><body><h1> Old Series </h1><ul>
<li class="productListItem" id="product-list-item-B0AAAAAAA1" aria-label="First Book">
  <div class="adbl-asin-impression"><img src="https://img/x_SL500_.jpg"></div>
  <h2>Book 1</h2>
  <a href="/pd/First-Book-Audiobook/B0AAAAAAA1?ref=x">link</a>
  <ul><li class="releaseDateLabel"><span>Release date: 01-15-20</span></li></ul>
</li>
<li class="productListItem" id="product-list-item-B0AAAAAAA2" aria-label="">
  <h2>Book 2.5</h2>
</li>
<li class="productListItem" id="not-an-asin"><h2>Book 3</h2></li>
<li class="productListItem" id="product-list-item-B0AAAAAAA1" aria-label="First Book">
  <h2>Some callout</h2>
</li>
</ul></body></html>
"""


def new_row(asin, title, header, date=None, img=True, href=None):
    script = ""
    if date:
        script = f'<script type="application/json">{json.dumps({"releaseDate": date})}</script>'
    image = '<img src="https://img/y_SL300_.jpg">' if img else ""
    href = href or f"/pd/{title.replace(' ', '-')}/{asin}?qid=1"
    return (f'<adbl-product-row series-header="{header}"><a href="{href}">x</a>'
            f'<h3 slot="title">{title}</h3>{image}{script}</adbl-product-row>')


NEW_HTML = (
    "<html><body><h1>New Series</h1>"
    + new_row("B0NNNNNNN1", "Alpha", "Book 1", "2021-03-04")
    + new_row("B0NNNNNNN2", "Beta", "Series, Book 2", None, img=False)
    + '<adbl-product-row><span>no link</span></adbl-product-row>'
    + '<adbl-product-row><a href="/pd/short/ABC">x</a></adbl-product-row>'
    + "</body></html>"
)


# ---------- dataclasses ----------

def test_dataclasses():
    b = ScrapedBook("A", "t", 1.0, datetime.date(2020, 1, 1), "u", None)
    s = ScrapedSeries("S", "n", "u", [b])
    r = SeriesSearchResult("S", "n", "u", None, "sample")
    assert s.books == [b] and b.image_url is None and r.author is None
    assert b == ScrapedBook("A", "t", 1.0, datetime.date(2020, 1, 1), "u", None)
    assert issubclass(SeriesPageError, Exception)


# ---------- extract_series_asin / _series_url ----------

@pytest.mark.parametrize("value,expected", [
    ("B0ABCDEFGH", "B0ABCDEFGH"),
    ("  B0ABCDEFGH  ", "B0ABCDEFGH"),
    ("https://www.audible.com/series/Some-Name/B0ABCDEFGH", "B0ABCDEFGH"),
    ("https://www.audible.com/series/Some-Name/B0ABCDEFGH?x=1", "B0ABCDEFGH"),
])
def test_extract_series_asin_ok(value, expected):
    assert extract_series_asin(value) == expected


@pytest.mark.parametrize("value", ["", "lowercase1", "TOOSHORT", "https://www.audible.com/pd/x/B0ABCDEFGH",
                                   "https://example.com/"])
def test_extract_series_asin_invalid(value):
    with pytest.raises(SeriesPageError):
        extract_series_asin(value)


def test_series_url():
    assert _series_url("B0ABCDEFGH") == "https://www.audible.com/series/x/B0ABCDEFGH"


# ---------- date / number helpers ----------

def test_parse_release_date():
    assert _parse_release_date("Release date: 01-15-20") == datetime.date(2020, 1, 15)
    assert _parse_release_date("12-31-99") == datetime.date(2099, 12, 31)


def test_parse_release_date_none_and_invalid(caplog):
    assert _parse_release_date("no date here") is None
    assert _parse_release_date("") is None
    with caplog.at_level("WARNING"):
        assert _parse_release_date("13-45-20") is None
    assert "Unparseable" in caplog.text


def test_parse_new_template_release_date():
    tag = soup_of('<script type="application/json">{"releaseDate": "2022-05-06"}</script>').script
    assert _parse_new_template_release_date(tag) == datetime.date(2022, 5, 6)


@pytest.mark.parametrize("content", [
    "not json", '{"other": 1}', '{"releaseDate": "garbage"}', '{"releaseDate": 5}', "[]", "",
])
def test_parse_new_template_release_date_bad(content):
    tag = soup_of(f'<script type="application/json">{content}</script>').script
    assert _parse_new_template_release_date(tag) is None


def test_parse_new_template_release_date_no_tag():
    assert _parse_new_template_release_date(None) is None


@pytest.mark.parametrize("text,expected", [
    ("Book 1", 1.0), ("Series, Book 2.5", 2.5), ("Book   10", 10.0),
    ("Prequel", None), ("", None), ("Book 1.2.3", None),
])
def test_extract_book_number(text, expected):
    assert _extract_book_number(text) == expected


def test_parse_position():
    assert _parse_position(soup_of("<li><h2>Book 4</h2></li>").li) == 4.0
    assert _parse_position(soup_of("<li><h2>Extra</h2></li>").li) is None
    assert _parse_position(soup_of("<li><p>Book 4</p></li>").li) is None


# ---------- dedupe ----------

def test_dedupe_prefers_positioned_and_keeps_order():
    a = book("A", None, "B000000001")
    b = book("B", 2.0, "B000000002")
    a2 = book("A", 1.0, "B000000003")
    b2 = book("B", None, "B000000004")
    assert _dedupe_by_title([a, b, a2, b2]) == [a2, b]


def test_dedupe_keeps_first_when_both_unpositioned_or_positioned():
    a, a2 = book("A", None, "B000000001"), book("A", None, "B000000002")
    assert _dedupe_by_title([a, a2]) == [a]
    p, p2 = book("A", 1.0, "B000000001"), book("A", 2.0, "B000000002")
    assert _dedupe_by_title([p, p2]) == [p]


def test_dedupe_empty():
    assert _dedupe_by_title([]) == []


# ---------- templates ----------

def test_parse_old_template():
    books = _parse_old_template(soup_of(OLD_HTML), "https://fallback")
    assert [b.asin for b in books] == ["B0AAAAAAA1", "B0AAAAAAA2", "B0AAAAAAA1"]
    first, second, _ = books
    assert first.title == "First Book"
    assert first.position == 1.0
    assert first.release_date == datetime.date(2020, 1, 15)
    assert first.url == "https://www.audible.com/pd/First-Book-Audiobook/B0AAAAAAA1"
    assert first.image_url == "https://img/x_SL120_.jpg"
    # missing aria-label -> ASIN title; no link -> fallback; no date/img
    assert second.title == "B0AAAAAAA2"
    assert second.position == 2.5
    assert second.url == "https://fallback"
    assert second.release_date is None and second.image_url is None


def test_parse_old_template_empty():
    assert _parse_old_template(soup_of("<html></html>"), "f") == []


def test_parse_old_template_img_without_src():
    html = ('<li class="productListItem" id="product-list-item-B0AAAAAAA1" aria-label="X">'
            '<div class="adbl-asin-impression"><img></div></li>')
    assert _parse_old_template(soup_of(html), "f")[0].image_url is None


def test_parse_new_template():
    books = _parse_new_template(soup_of(NEW_HTML))
    assert [b.asin for b in books] == ["B0NNNNNNN1", "B0NNNNNNN2"]
    a, b = books
    assert a.title == "Alpha" and a.position == 1.0
    assert a.release_date == datetime.date(2021, 3, 4)
    assert a.url == "https://www.audible.com/pd/Alpha/B0NNNNNNN1"
    assert a.image_url == "https://img/y_SL120_.jpg"
    assert b.position == 2.0 and b.release_date is None and b.image_url is None


def test_parse_new_template_title_fallback_and_no_series_header():
    html = ('<adbl-product-row><a href="/pd/x/B0NNNNNNN3">x</a></adbl-product-row>')
    b = _parse_new_template(soup_of(html))[0]
    assert b.title == "B0NNNNNNN3" and b.position is None


def test_parse_new_template_empty():
    assert _parse_new_template(soup_of("<html></html>")) == []


# ---------- parse_series_page ----------

def test_parse_series_page_old():
    s = parse_series_page(OLD_HTML, SERIES_URL)
    assert s.name == "Old Series"
    assert s.asin == "B0SERIES01"
    assert s.url == SERIES_URL
    # duplicate title deduped, positioned entry kept
    assert [b.asin for b in s.books] == ["B0AAAAAAA1", "B0AAAAAAA2"]
    assert s.books[0].position == 1.0


def test_parse_series_page_new():
    s = parse_series_page(NEW_HTML, SERIES_URL)
    assert s.name == "New Series"
    assert [b.title for b in s.books] == ["Alpha", "Beta"]


def test_parse_series_page_old_takes_precedence_over_new():
    html = OLD_HTML + new_row("B0NNNNNNN1", "Alpha", "Book 1")
    s = parse_series_page(html, SERIES_URL)
    assert {b.asin for b in s.books} == {"B0AAAAAAA1", "B0AAAAAAA2"}


def test_parse_series_page_no_h1_and_odd_fallback_url():
    html = new_row("B0NNNNNNN1", "Alpha", "Book 1")
    s = parse_series_page(html, "not-a-url")
    assert s.name == "Unknown series"
    assert s.asin == "not-a-url"


@pytest.mark.parametrize("html", ["", "<html><body><h1>Empty</h1></body></html>",
                                  "<html><body>Access denied</body></html>"])
def test_parse_series_page_empty_raises(html):
    with pytest.raises(SeriesPageError, match="No books found"):
        parse_series_page(html, SERIES_URL)


# ---------- fetch_series ----------

def test_fetch_series_with_asin(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "_curl_get", lambda url, params=None: calls.append(url) or OLD_HTML)
    s = fetch_series("B0SERIES01")
    assert calls == ["https://www.audible.com/series/x/B0SERIES01"]
    assert s.asin == "B0SERIES01" and s.url == calls[0]


def test_fetch_series_with_url(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "_curl_get", lambda url, params=None: calls.append(url) or NEW_HTML)
    s = fetch_series(SERIES_URL)
    assert calls == [SERIES_URL]
    assert s.url == SERIES_URL and len(s.books) == 2


def test_fetch_series_invalid_input_skips_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network called")

    monkeypatch.setattr(scraper, "_curl_get", boom)
    with pytest.raises(SeriesPageError):
        fetch_series("nonsense")


def test_fetch_series_propagates_errors(monkeypatch):
    def fail(url, params=None):
        raise SeriesPageError("HTTP 503")

    monkeypatch.setattr(scraper, "_curl_get", fail)
    with pytest.raises(SeriesPageError, match="503"):
        fetch_series("B0SERIES01")


# ---------- name / matching ----------

@pytest.mark.parametrize("slug,expected", [
    ("The-Stormlight-Archive-Audiobooks", "The Stormlight Archive"),
    ("Dune-audiobooks", "Dune"),
    ("Dune", "Dune"),
    ("", ""),
])
def test_name_from_slug(slug, expected):
    assert _name_from_slug(slug) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Harry Potter's  World!", "harry potters world"),
    ("A--B__C", "a b c"),
    ("  UPPER  ", "upper"),
    ("", ""),
])
def test_normalize_for_match(raw, expected):
    assert _normalize_for_match(raw) == expected


def test_find_best_match_exact_beats_substring():
    exact, sub = sr("Dune", "B000000001"), sr("Dune Messiah Saga", "B000000002")
    assert find_best_match("dune", [sub, exact]) is exact


def test_find_best_match_multiple_substring_matches_is_ambiguous():
    a, b = sr("Dune Chronicles", "B000000001"), sr("Dune Chronicles Extended Collection", "B000000002")
    assert find_best_match("Dune", [b, a]) is None


def test_find_best_match_result_contained_in_target():
    r = sr("Dune")
    assert find_best_match("Dune Chronicles", [r]) is r


def test_find_best_match_ambiguous_tie_returns_none():
    a, b = sr("Dune", "B000000001"), sr("DUNE!", "B000000002")
    assert find_best_match("dune", [a, b]) is None


def test_find_best_match_no_candidates():
    assert find_best_match("Dune", []) is None
    assert find_best_match("Dune", [sr("Foundation")]) is None


def test_find_best_match_skips_foreign_editions():
    fr = sr("Dune French Edition")
    assert find_best_match("Dune", [fr]) is None
    native = sr("Dune Saga")
    assert find_best_match("Dune", [fr, native]) is native


def test_find_best_match_allows_foreign_when_target_foreign():
    fr = sr("Dune French")
    assert find_best_match("Dune French", [fr]) is fr


# ---------- search_series ----------

SEARCH_HTML = """
<ul>
<li class="productListItem" aria-label=" Sample One ">
  <a href="/series/Alpha-Saga-Audiobooks/B0SSSSSSS1?ref=1">s</a>
  <a href="/author/Jane-Doe/B0AUTHOR01">Jane Doe</a>
</li>
<li class="productListItem" aria-label="Sample Dup">
  <a href="/series/Alpha-Saga-Audiobooks/B0SSSSSSS1">s</a>
</li>
<li class="productListItem" aria-label="Sample Two">
  <a href="/series/Beta-Audiobooks/B0SSSSSSS2">s</a>
</li>
<li class="productListItem" aria-label="No series"><a href="/pd/x/B0PPPPPPP1">p</a></li>
<li class="productListItem" aria-label="Bad"><a href="/series/oops">s</a></li>
</ul>
"""


def test_search_series(monkeypatch):
    seen = {}

    def fake(url, params=None):
        seen["url"], seen["params"] = url, params
        return SEARCH_HTML

    monkeypatch.setattr(scraper, "_curl_get", fake)
    results = search_series("alpha saga")
    assert seen == {"url": "https://www.audible.com/search", "params": {"keywords": "alpha saga"}}
    assert [r.asin for r in results] == ["B0SSSSSSS1", "B0SSSSSSS2"]
    first, second = results
    assert first.name == "Alpha Saga"
    assert first.url == "https://www.audible.com/series/Alpha-Saga-Audiobooks/B0SSSSSSS1"
    assert first.author == "Jane Doe"
    assert first.sample_title == "Sample One"
    assert second.author is None and second.name == "Beta"


def test_search_series_empty(monkeypatch):
    monkeypatch.setattr(scraper, "_curl_get", lambda url, params=None: "<html></html>")
    assert search_series("x") == []


def test_search_series_propagates_errors(monkeypatch):
    def fail(url, params=None):
        raise SeriesPageError("nope")

    monkeypatch.setattr(scraper, "_curl_get", fail)
    with pytest.raises(SeriesPageError):
        search_series("x")


# ---------- _curl_get ----------

def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def test_curl_get_success(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"], captured["kw"] = cmd, kw
        return completed("<html>body</html>\n__HTTP_STATUS__200")

    monkeypatch.setattr(subprocess, "run", fake_run)
    body = _curl_get("https://x.test/p")
    assert body == "<html>body</html>\n"
    assert captured["cmd"][0] == "curl"
    assert captured["cmd"][-1] == "https://x.test/p"
    assert scraper.USER_AGENT in captured["cmd"]
    assert captured["kw"]["timeout"] == 30 and captured["kw"]["capture_output"] is True


def test_curl_get_params_encoded(monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["url"] = cmd[-1]
        return completed("ok__HTTP_STATUS__204")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _curl_get("https://x.test/s", params={"keywords": "a b&c"}) == "ok"
    assert captured["url"] == "https://x.test/s?keywords=a+b%26c"


def test_curl_get_non_2xx(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed("blocked\n__HTTP_STATUS__503"))
    with pytest.raises(SeriesPageError, match="HTTP 503"):
        _curl_get("https://x.test/")


def test_curl_get_missing_status_marker(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed("no marker"))
    with pytest.raises(SeriesPageError, match="HTTP"):
        _curl_get("https://x.test/")


def test_curl_get_nonzero_exit(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed("", 6, " Could not resolve host\n"))
    with pytest.raises(SeriesPageError, match=r"curl failed \(6\).*Could not resolve host"):
        _curl_get("https://x.test/")


def test_curl_get_timeout(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="curl", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SeriesPageError, match="timed out") as ei:
        _curl_get("https://x.test/")
    assert isinstance(ei.value.__cause__, subprocess.TimeoutExpired)


def test_curl_get_missing_binary_propagates(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("curl")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(FileNotFoundError):
        _curl_get("https://x.test/")
