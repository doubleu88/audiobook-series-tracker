import json

import httpx
import pytest

from app import prowlarr as prowlarr_mod
from app.prowlarr import AUDIOBOOK_CATEGORY, ProwlarrClient, ProwlarrError, ProwlarrResult


def _install(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    real_client = httpx.Client

    def fake_request(method, url, **kwargs):
        kwargs.pop("timeout", None)
        with real_client(transport=transport) as c:
            return c.request(method, url, **kwargs)

    monkeypatch.setattr(prowlarr_mod.httpx, "request", fake_request)
    return calls


@pytest.fixture
def client():
    return ProwlarrClient("http://prowlarr.local/", "KEY")


def test_prowlarr_error_is_exception():
    with pytest.raises(ProwlarrError, match="x"):
        raise ProwlarrError("x")


def test_result_dataclass():
    r = ProwlarrResult("g", 1, "Idx", "T", 10, None, "torrent", None)
    assert r.guid == "g" and r.seeders is None and r.publish_date is None
    assert r == ProwlarrResult("g", 1, "Idx", "T", 10, None, "torrent", None)


def test_init_strips_slash(client):
    assert client.base_url == "http://prowlarr.local"
    assert client.api_key == "KEY"


class TestRequest:
    def test_success_headers_and_json(self, monkeypatch, client):
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json=[1, 2]))
        assert client._request("GET", "/api/v1/x", params={"q": "a"}) == [1, 2]
        assert calls[0].headers["X-Api-Key"] == "KEY"
        assert str(calls[0].url) == "http://prowlarr.local/api/v1/x?q=a"

    def test_passes_json_body(self, monkeypatch, client):
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json={}))
        client._request("POST", "/p", json={"a": 1})
        assert calls[0].method == "POST"
        assert json.loads(calls[0].content) == {"a": 1}

    def test_401(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ProwlarrError, match="rejected the API key"):
            client._request("GET", "/x")

    @pytest.mark.parametrize("status", [400, 404, 500, 502])
    def test_http_errors(self, monkeypatch, client, status):
        _install(monkeypatch, lambda r: httpx.Response(status, text="bad"))
        with pytest.raises(ProwlarrError, match=f"HTTP {status} for /x"):
            client._request("GET", "/x")

    def test_timeout(self, monkeypatch, client):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ProwlarrError, match="Could not reach Prowlarr at http://prowlarr.local"):
            client._request("GET", "/x")

    def test_connect_error(self, monkeypatch, client):
        def handler(request):
            raise httpx.ConnectError("no", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ProwlarrError, match="Could not reach"):
            client._request("GET", "/x")

    def test_malformed_json(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(200, text="oops"))
        with pytest.raises(ProwlarrError, match="non-JSON response for /x"):
            client._request("GET", "/x")


class TestSearch:
    def test_parses_and_sorts_by_seeders(self, monkeypatch, client):
        rows = [
            {"guid": "g1", "indexerId": 1, "indexer": "A", "title": "T1", "size": 5, "seeders": 3,
             "protocol": "torrent", "publishDate": "2024-01-01T00:00:00Z"},
            {"guid": "g2", "indexerId": 2, "indexer": "B", "title": "T2", "size": 6, "seeders": 50,
             "protocol": "torrent", "publishDate": None},
            {"guid": "g3", "indexerId": 3, "seeders": None},
        ]
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json=rows))
        results = client.search("dune")
        assert [r.guid for r in results] == ["g2", "g1", "g3"]
        assert results[0] == ProwlarrResult("g2", 2, "B", "T2", 6, 50, "torrent", None)
        assert results[1].publish_date == "2024-01-01T00:00:00Z"
        req = calls[0]
        assert req.url.path == "/api/v1/search"
        assert dict(req.url.params) == {"query": "dune", "type": "search", "categories": str(AUDIOBOOK_CATEGORY)}

    def test_defaults_for_missing_fields(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(200, json=[{"guid": "g", "indexerId": 9}]))
        (r,) = client.search("q")
        assert r == ProwlarrResult("g", 9, "Unknown indexer", "Untitled", 0, None, "unknown", None)

    def test_empty(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(200, json=[]))
        assert client.search("q") == []

    def test_zero_seeders_and_none_tie(self, monkeypatch, client):
        rows = [
            {"guid": "a", "indexerId": 1, "seeders": None},
            {"guid": "b", "indexerId": 1, "seeders": 0},
            {"guid": "c", "indexerId": 1, "seeders": 1},
        ]
        _install(monkeypatch, lambda r: httpx.Response(200, json=rows))
        results = client.search("q")
        assert results[0].guid == "c"
        assert {r.guid for r in results[1:]} == {"a", "b"}

    def test_missing_required_key_raises_keyerror(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(200, json=[{"indexerId": 1}]))
        with pytest.raises(KeyError):
            client.search("q")

    def test_errors_propagate(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ProwlarrError):
            client.search("q")


class TestGrab:
    def test_posts_guid_and_indexer(self, monkeypatch, client):
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert client.grab("guid-1", 7) is None
        req = calls[0]
        assert req.method == "POST"
        assert req.url.path == "/api/v1/search"
        assert json.loads(req.content) == {"guid": "guid-1", "indexerId": 7}

    def test_http_error(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(500))
        with pytest.raises(ProwlarrError, match="HTTP 500"):
            client.grab("g", 1)

    def test_timeout(self, monkeypatch, client):
        def handler(request):
            raise httpx.ReadTimeout("t", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ProwlarrError, match="Could not reach"):
            client.grab("g", 1)


class TestTestConnection:
    def test_success(self, monkeypatch, client):
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json=[]))
        assert client.test_connection() is None
        assert calls[0].url.path == "/api/v1/indexer"
        assert calls[0].method == "GET"

    def test_auth_failure(self, monkeypatch, client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ProwlarrError, match="API key"):
            client.test_connection()

    def test_unreachable(self, monkeypatch, client):
        def handler(request):
            raise httpx.ConnectError("x", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ProwlarrError):
            client.test_connection()
