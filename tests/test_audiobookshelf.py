import httpx
import pytest

from app import audiobookshelf as abs_mod
from app.audiobookshelf import ABSClient, ABSError, ABSLibrary


def _install(monkeypatch, handler):
    """Route both httpx.get and httpx.Client through a MockTransport."""
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    real_client = httpx.Client

    def fake_get(url, **kwargs):
        with real_client(transport=transport) as c:
            return c.get(url, **kwargs)

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(abs_mod.httpx, "get", fake_get)
    monkeypatch.setattr(abs_mod.httpx, "Client", fake_client)
    return calls


@pytest.fixture
def abs_client():
    return ABSClient("http://abs.local/", "KEY")


def test_abserror_is_exception():
    assert issubclass(ABSError, Exception)
    with pytest.raises(ABSError, match="boom"):
        raise ABSError("boom")


def test_abslibrary_dataclass():
    lib = ABSLibrary(id="l1", name="Books")
    assert (lib.id, lib.name) == ("l1", "Books")
    assert lib == ABSLibrary("l1", "Books")


def test_init_strips_trailing_slash(abs_client):
    assert abs_client.base_url == "http://abs.local"
    assert abs_client.api_key == "KEY"


class TestGet:
    def test_success_sends_auth_and_params(self, monkeypatch, abs_client):
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
        assert abs_client._get("/api/x", params={"a": 1}) == {"ok": 1}
        req = calls[0]
        assert str(req.url) == "http://abs.local/api/x?a=1"
        assert req.headers["Authorization"] == "Bearer KEY"

    def test_uses_provided_client(self, abs_client):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"v": 2})

        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            assert abs_client._get("/p", client=c) == {"v": 2}
        assert len(seen) == 1

    def test_401(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ABSError, match="rejected the API key"):
            abs_client._get("/api/x")

    @pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
    def test_http_errors(self, monkeypatch, abs_client, status):
        _install(monkeypatch, lambda r: httpx.Response(status, text="nope"))
        with pytest.raises(ABSError, match=f"HTTP {status} for /api/x"):
            abs_client._get("/api/x")

    def test_timeout(self, monkeypatch, abs_client):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ABSError, match="Could not reach Audiobookshelf at http://abs.local"):
            abs_client._get("/api/x")

    def test_connect_error(self, monkeypatch, abs_client):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ABSError, match="Could not reach"):
            abs_client._get("/api/x")

    def test_malformed_json(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, text="<html>not json"))
        with pytest.raises(ABSError, match="non-JSON response for /api/x"):
            abs_client._get("/api/x")


class TestListLibraries:
    def test_filters_book_libraries(self, monkeypatch, abs_client):
        payload = {
            "libraries": [
                {"id": "a", "name": "Audiobooks", "mediaType": "book"},
                {"id": "b", "name": "Pods", "mediaType": "podcast"},
                {"id": "c", "name": "More", "mediaType": "book"},
            ]
        }
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json=payload))
        libs = abs_client.list_libraries()
        assert libs == [ABSLibrary("a", "Audiobooks"), ABSLibrary("c", "More")]
        assert calls[0].url.path == "/api/libraries"

    def test_empty_and_missing_key(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert abs_client.list_libraries() == []

    def test_error_propagates(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ABSError):
            abs_client.list_libraries()


def _item(asin):
    return {"media": {"metadata": {"asin": asin}}}


class TestListAsins:
    def test_single_page_uppercases_and_skips_missing(self, monkeypatch, abs_client):
        results = [
            _item("b0abc"),
            _item("B0DEF"),
            _item(None),
            {"media": None},
            {"media": {"metadata": {}}},
            {},
            _item("B0ABC"),  # duplicate after uppercase
        ]
        calls = _install(monkeypatch, lambda r: httpx.Response(200, json={"results": results, "total": 7}))
        progress = []
        asins = abs_client.list_asins_in_library("lib1", progress_cb=lambda *a: progress.append(a))
        assert asins == {"B0ABC", "B0DEF"}
        assert calls[0].url.path == "/api/libraries/lib1/items"
        assert dict(calls[0].url.params) == {"minified": "1", "limit": "500", "page": "0"}
        assert progress == [(1, 1, 2, 7)]

    def test_multiple_pages(self, monkeypatch, abs_client):
        monkeypatch.setattr(abs_mod, "PAGE_SIZE", 2)
        pages = {
            "0": [_item("A1"), _item("A2")],
            "1": [_item("A3"), _item("A4")],
            "2": [_item("A5")],
        }

        def handler(request):
            return httpx.Response(200, json={"results": pages[request.url.params["page"]], "total": 5})

        calls = _install(monkeypatch, handler)
        progress = []
        asins = abs_client.list_asins_in_library("L", progress_cb=lambda *a: progress.append(a))
        assert asins == {"A1", "A2", "A3", "A4", "A5"}
        assert len(calls) == 3
        assert progress == [(1, 3, 2, 5), (2, 3, 4, 5), (3, 3, 5, 5)]

    def test_no_total_gives_none(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={"results": [_item("X")]}))
        progress = []
        abs_client.list_asins_in_library("L", progress_cb=lambda *a: progress.append(a))
        assert progress == [(1, None, 1, None)]

    def test_non_int_total_ignored(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "total": "12"}))
        progress = []
        assert abs_client.list_asins_in_library("L", progress_cb=lambda *a: progress.append(a)) == set()
        assert progress == [(1, None, 0, None)]

    def test_zero_total_one_page(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={"results": [], "total": 0}))
        progress = []
        abs_client.list_asins_in_library("L", progress_cb=lambda *a: progress.append(a))
        assert progress == [(1, 1, 0, 0)]

    def test_without_progress_cb(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={"results": [_item("z1")]}))
        assert abs_client.list_asins_in_library("L") == {"Z1"}

    def test_missing_results_key(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert abs_client.list_asins_in_library("L") == set()

    def test_error_mid_pagination(self, monkeypatch, abs_client):
        monkeypatch.setattr(abs_mod, "PAGE_SIZE", 1)

        def handler(request):
            if request.url.params["page"] == "0":
                return httpx.Response(200, json={"results": [_item("A")]})
            return httpx.Response(500)

        _install(monkeypatch, handler)
        with pytest.raises(ABSError, match="HTTP 500"):
            abs_client.list_asins_in_library("L")

    def test_timeout(self, monkeypatch, abs_client):
        def handler(request):
            raise httpx.ConnectTimeout("t", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ABSError, match="Could not reach"):
            abs_client.list_asins_in_library("L")


class TestTestConnection:
    def test_success(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(200, json={"libraries": []}))
        assert abs_client.test_connection() is None

    def test_auth_failure(self, monkeypatch, abs_client):
        _install(monkeypatch, lambda r: httpx.Response(401))
        with pytest.raises(ABSError, match="API key"):
            abs_client.test_connection()

    def test_unreachable(self, monkeypatch, abs_client):
        def handler(request):
            raise httpx.ConnectError("x", request=request)

        _install(monkeypatch, handler)
        with pytest.raises(ABSError):
            abs_client.test_connection()
