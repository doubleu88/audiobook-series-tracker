import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 45.0
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

    def _get(self, path: str, params: dict | None = None, client: httpx.Client | None = None) -> dict:
        caller = client.get if client is not None else httpx.get
        try:
            response = caller(
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

    def list_asins_in_library(
        self,
        library_id: str,
        progress_cb: Callable[[int, int | None, int, int | None], None] | None = None,
    ) -> set[str]:
        asins: set[str] = set()
        page = 0
        total_items: int | None = None
        total_pages: int | None = None
        with httpx.Client(timeout=TIMEOUT) as client:
            while True:
                data = self._get(
                    f"/api/libraries/{library_id}/items",
                    params={"minified": 1, "limit": PAGE_SIZE, "page": page},
                    client=client,
                )
                items = data.get("results", [])
                if total_items is None and "total" in data and isinstance(data["total"], int):
                    total_items = data["total"]
                    total_pages = math.ceil(total_items / PAGE_SIZE) if total_items > 0 else 1

                for item in items:
                    asin = (item.get("media") or {}).get("metadata", {}).get("asin")
                    if asin:
                        asins.add(asin.upper())

                logger.debug(
                    "Audiobookshelf library %s: page %d returned %d items (%d ASINs so far)",
                    library_id, page, len(items), len(asins),
                )
                if progress_cb:
                    progress_cb(page + 1, total_pages, len(asins), total_items)
                if len(items) < PAGE_SIZE:
                    break
                page += 1
        return asins

    def test_connection(self) -> None:
        self.list_libraries()
