import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 20.0
AUDIOBOOK_CATEGORY = 3030  # Newznab/Torznab "Audio/Audiobook" — confirmed against a
# live Prowlarr instance during implementation (AudioBook Bay indexer tagged its
# results with this category id).


class ProwlarrError(Exception):
    pass


@dataclass
class ProwlarrResult:
    guid: str
    indexer_id: int
    indexer_name: str
    title: str
    size: int
    seeders: int | None
    protocol: str
    publish_date: str | None


class ProwlarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"X-Api-Key": self.api_key},
                timeout=TIMEOUT,
                **kwargs,
            )
        except httpx.RequestError as exc:
            logger.debug("Prowlarr request to %s %s%s failed: %r", method, self.base_url, path, exc)
            raise ProwlarrError(f"Could not reach Prowlarr at {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise ProwlarrError("Prowlarr rejected the API key (401 Unauthorized).")
        if response.status_code >= 400:
            logger.debug(
                "Prowlarr %s %s -> HTTP %s: %s",
                method, path, response.status_code, response.text[:500],
            )
            raise ProwlarrError(f"Prowlarr returned HTTP {response.status_code} for {path}.")

        try:
            return response.json()
        except ValueError as exc:
            raise ProwlarrError(f"Prowlarr returned a non-JSON response for {path}.") from exc

    def search(self, query: str) -> list[ProwlarrResult]:
        data = self._request(
            "GET",
            "/api/v1/search",
            params={"query": query, "type": "search", "categories": AUDIOBOOK_CATEGORY},
        )
        results = []
        for row in data:
            results.append(
                ProwlarrResult(
                    guid=row["guid"],
                    indexer_id=row["indexerId"],
                    indexer_name=row.get("indexer", "Unknown indexer"),
                    title=row.get("title", "Untitled"),
                    size=row.get("size", 0),
                    seeders=row.get("seeders"),
                    protocol=row.get("protocol", "unknown"),
                    publish_date=row.get("publishDate"),
                )
            )
        results.sort(key=lambda r: (r.seeders or 0), reverse=True)
        return results

    def grab(self, guid: str, indexer_id: int) -> None:
        self._request("POST", "/api/v1/search", json={"guid": guid, "indexerId": indexer_id})

    def test_connection(self) -> None:
        self._request("GET", "/api/v1/indexer")
