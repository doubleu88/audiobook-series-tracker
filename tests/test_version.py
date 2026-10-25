import httpx
import pytest

from app import version


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setitem(version._cache, "latest", None)
    monkeypatch.setitem(version._cache, "checked_at", 0.0)
    monkeypatch.setattr(version, "CURRENT_VERSION", "1.2.3")


class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=False):
        self.payload, self.status, self.json_error = payload, status, json_error

    def raise_for_status(self):
        if self.status >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        if self.json_error:
            raise ValueError("not json")
        return self.payload


def patch_get(monkeypatch, response=None, exc=None):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if exc:
            raise exc
        return response

    monkeypatch.setattr(version.httpx, "get", fake_get)
    return calls


# _read_current_version
def test_read_current_version(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [2.4.6] - 2026-01-02\n\n- x\n\n## [1.0.0] - 2025-01-01\n"
    )
    monkeypatch.chdir(tmp_path)
    assert version._read_current_version() == "2.4.6"


def test_read_current_version_missing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert version._read_current_version() == "unknown"


def test_read_current_version_no_match(tmp_path, monkeypatch):
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nnothing here\n")
    monkeypatch.chdir(tmp_path)
    assert version._read_current_version() == "unknown"


def test_real_changelog_parses():
    assert version._read_current_version() != "unknown"


# _fetch_latest_release_version
def test_fetch_strips_v_prefix(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse({"tag_name": "v1.5.0"}))
    assert version._fetch_latest_release_version() == "1.5.0"
    assert version.GITHUB_REPO in calls[0][0]


def test_fetch_no_prefix(monkeypatch):
    patch_get(monkeypatch, FakeResponse({"tag_name": "3.0.1"}))
    assert version._fetch_latest_release_version() == "3.0.1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exc": httpx.ConnectError("down")},
        {"response": FakeResponse(status=500)},
        {"response": FakeResponse({"nope": 1})},
        {"response": FakeResponse(json_error=True)},
    ],
)
def test_fetch_failure_returns_none(monkeypatch, kwargs):
    patch_get(monkeypatch, **kwargs)
    assert version._fetch_latest_release_version() is None


# _version_tuple
def test_version_tuple():
    assert version._version_tuple("1.10.2") == (1, 10, 2)
    assert version._version_tuple("1.10.0") > version._version_tuple("1.9.9")


def test_version_tuple_invalid():
    with pytest.raises(ValueError):
        version._version_tuple("unknown")


# get_update_status
def test_update_available(monkeypatch):
    patch_get(monkeypatch, FakeResponse({"tag_name": "v1.3.0"}))
    status = version.get_update_status()
    assert status["current"] == "1.2.3"
    assert status["latest"] == "1.3.0"
    assert status["update_available"] is True
    assert status["release_url"] == f"https://github.com/{version.GITHUB_REPO}/releases/latest"


def test_no_update_when_same_or_older(monkeypatch):
    patch_get(monkeypatch, FakeResponse({"tag_name": "v1.2.3"}))
    assert version.get_update_status()["update_available"] is False
    monkeypatch.setitem(version._cache, "checked_at", 0.0)
    patch_get(monkeypatch, FakeResponse({"tag_name": "v1.0.0"}))
    assert version.get_update_status()["update_available"] is False


def test_cached_within_interval(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse({"tag_name": "v9.0.0"}))
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0)
    version.get_update_status()
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0 + version.CHECK_INTERVAL_SECONDS - 1)
    version.get_update_status()
    assert len(calls) == 1


def test_refetch_after_interval(monkeypatch):
    calls = patch_get(monkeypatch, FakeResponse({"tag_name": "v9.0.0"}))
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0)
    version.get_update_status()
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0 + version.CHECK_INTERVAL_SECONDS + 1)
    version.get_update_status()
    assert len(calls) == 2


def test_failure_keeps_last_known(monkeypatch):
    patch_get(monkeypatch, FakeResponse({"tag_name": "v5.0.0"}))
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0)
    version.get_update_status()
    patch_get(monkeypatch, exc=httpx.ConnectError("down"))
    monkeypatch.setattr(version.time, "time", lambda: 1_000_000.0 + version.CHECK_INTERVAL_SECONDS + 1)
    status = version.get_update_status()
    assert status["latest"] == "5.0.0"
    assert status["update_available"] is True


def test_failure_never_succeeded(monkeypatch):
    patch_get(monkeypatch, exc=httpx.ConnectError("down"))
    status = version.get_update_status()
    assert status["latest"] is None
    assert status["update_available"] is False


def test_unknown_current_version_no_update(monkeypatch):
    monkeypatch.setattr(version, "CURRENT_VERSION", "unknown")
    patch_get(monkeypatch, FakeResponse({"tag_name": "v1.0.0"}))
    status = version.get_update_status()
    assert status["latest"] == "1.0.0"
    assert status["update_available"] is False
