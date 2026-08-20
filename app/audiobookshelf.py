import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 15.0
PAGE_SIZE = 500


class ABSError(Exception):
    pass


@dataclass
class ABSLibrary:
    id: str
    name: str


class ABSClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=TIMEOUT,
            )
        except httpx.RequestError as exc:
            logger.debug("Audiobookshelf request to %s%s failed: %r", self.base_url, path, exc)
            raise ABSError(f"Could not reach Audiobookshelf at {self.base_url}: {exc}") from exc

        if response.status_code == 401:
            raise ABSError("Audiobookshelf rejected the API key (401 Unauthorized).")
        if response.status_code >= 400:
            logger.debug(
                "Audiobookshelf GET %s -> HTTP %s: %s",
                path, response.status_code, response.text[:500],
            )
            raise ABSError(f"Audiobookshelf returned HTTP {response.status_code} for {path}.")

        try:
            return response.json()
        except ValueError as exc:
            raise ABSError(f"Audiobookshelf returned a non-JSON response for {path}.") from exc

    def list_libraries(self) -> list[ABSLibrary]:
        data = self._get("/api/libraries")
        return [
            ABSLibrary(id=lib["id"], name=lib["name"])
            for lib in data.get("libraries", [])
            if lib.get("mediaType") == "book"
        ]

    def list_asins_in_library(self, library_id: str) -> set[str]:
        asins: set[str] = set()
        page = 0
        while True:
            data = self._get(
                f"/api/libraries/{library_id}/items",
                params={"minified": 1, "limit": PAGE_SIZE, "page": page},
            )
            items = data.get("results", [])
            for item in items:
                asin = (item.get("media") or {}).get("metadata", {}).get("asin")
                if asin:
                    asins.add(asin.upper())
            if len(items) < PAGE_SIZE:
                break
            page += 1
        return asins

    def test_connection(self) -> None:
        self.list_libraries()
