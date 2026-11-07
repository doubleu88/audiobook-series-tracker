from unittest.mock import MagicMock, patch

import httpx
import pytest

from app import scraper
from app.scraper import SeriesPageError, search_series

SEARCH_PAYLOAD = {
    "products": [
        {
            "asin": "B0BOOK0001",
            "title": "Pit Fighter",
            "authors": [{"name": "MC Gallup"}, {"name": "Plum Parrot"}],
            "series": [{"asin": "B0SERIES01", "sequence": "1", "title": "Victor of Tucson"}],
        },
        {
            "asin": "B0BOOK0002",
            "title": "Champion",
            "authors": [{"name": "MC Gallup"}],
            "series": [{"asin": "B0SERIES01", "sequence": "3", "title": "Victor of Tucson"}],
        },
        {"asin": "B0BOOK0003", "title": "Standalone", "authors": [{"name": "Someone"}], "series": None},
    ]
}


def _response(status, payload=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload or {}
    return resp


def _client(*responses):
    client = MagicMock()
    client.get.side_effect = list(responses)
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    return ctx, client


def test_search_uses_catalog_api_and_dedupes_series():
    ctx, client = _client(_response(200, SEARCH_PAYLOAD), _response(200, {"products": []}))
    with patch.object(scraper.httpx, "Client", return_value=ctx), \
            patch.object(scraper, "_curl_get") as curl:
        results = search_series("victor of tucson")

    curl.assert_not_called()
    assert [r.asin for r in results] == ["B0SERIES01"]
    assert results[0].name == "Victor of Tucson"
    assert results[0].author == "MC Gallup, Plum Parrot"
    assert results[0].sample_title == "Pit Fighter"
    assert results[0].url == "https://www.audible.com/series/x/B0SERIES01"
    assert client.get.call_args_list[0].kwargs["params"]["keywords"] == "victor of tucson"


def test_search_enrichment_sets_book_count():
    enrich = {"products": [{
        "asin": "B0SERIES01",
        "title": "Victor of Tucson",
        "relationships": [{"relationship_type": "series"}] * 11,
        "authors": [{"name": "MC Gallup"}],
    }]}
    ctx, _ = _client(_response(200, SEARCH_PAYLOAD), _response(200, enrich))
    with patch.object(scraper.httpx, "Client", return_value=ctx):
        results = search_series("victor of tucson")
    assert results[0].book_count == 11
    assert results[0].author == "MC Gallup"


def test_empty_api_result_does_not_fall_back_to_html():
    ctx, _ = _client(_response(200, {"products": []}))
    with patch.object(scraper.httpx, "Client", return_value=ctx), \
            patch.object(scraper, "_curl_get") as curl:
        assert search_series("zzzz nothing") == []
    curl.assert_not_called()


@pytest.mark.parametrize("failure", [_response(503), httpx.ConnectError("boom")])
def test_api_failure_falls_back_to_html(failure):
    html = """
    <li class="productListItem" aria-label="Some Book">
      <a href="/series/Some-Series-Audiobooks/B0SERIES02?x=1">Some Series</a>
      <li class="authorLabel">By: Jane Doe</li>
    </li>"""
    ctx, _ = _client(failure)
    with patch.object(scraper.httpx, "Client", return_value=ctx), \
            patch.object(scraper, "_curl_get", return_value=html) as curl, \
            patch.object(scraper, "_enrich_search_results"):
        results = search_series("some series")
    curl.assert_called_once()
    assert [r.asin for r in results] == ["B0SERIES02"]
    assert results[0].name == "Some Series"


def test_both_paths_failing_raises():
    ctx, _ = _client(_response(503))
    with patch.object(scraper.httpx, "Client", return_value=ctx), \
            patch.object(scraper, "_curl_get", side_effect=SeriesPageError("Audible returned HTTP 503")):
        with pytest.raises(SeriesPageError):
            search_series("anything")
