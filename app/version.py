import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

GITHUB_REPO = "doubleu88/audiobook-series-tracker"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60

_TOP_ENTRY_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", re.M)


def _read_current_version() -> str:
    try:
        with open("CHANGELOG.md") as f:
            content = f.read()
    except FileNotFoundError:
        return "unknown"
    match = _TOP_ENTRY_RE.search(content)
    return match.group(1) if match else "unknown"


CURRENT_VERSION = _read_current_version()

_cache: dict = {"latest": None, "checked_at": 0.0}


def _fetch_latest_release_version() -> str | None:
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()["tag_name"].lstrip("v")
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        logger.warning("Could not check for a newer release: %s", exc)
        return None


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(part) for part in v.split("."))


def get_update_status() -> dict:
    """Cached check of whether a newer GitHub release exists.

    Refetches at most once per CHECK_INTERVAL_SECONDS; a failed check keeps
    whatever was last known (or None if it's never succeeded) rather than
    flapping the UI between states.
    """
    now = time.time()
    if now - _cache["checked_at"] > CHECK_INTERVAL_SECONDS:
        latest = _fetch_latest_release_version()
        if latest is not None:
            _cache["latest"] = latest
        _cache["checked_at"] = now

    latest = _cache["latest"]
    update_available = False
    if latest is not None:
        try:
            update_available = _version_tuple(latest) > _version_tuple(CURRENT_VERSION)
        except ValueError:
            update_available = False

    return {
        "current": CURRENT_VERSION,
        "latest": latest,
        "update_available": update_available,
        "release_url": f"https://github.com/{GITHUB_REPO}/releases/latest",
    }
