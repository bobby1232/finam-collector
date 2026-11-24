"""Read-only client for the private Collector API."""

import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from .contracts import SnapshotQuery


class MarketDataError(RuntimeError):
    pass


class MarketDataProvider(Protocol):
    async def snapshot(
        self, query: SnapshotQuery, *, require_fresh: bool = False
    ) -> dict[str, Any]: ...


class RestMarketDataClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 5, cache_seconds: float = 2):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
        )
        self._cache_seconds = max(0, min(cache_seconds, 30))
        self._key = ""
        self._cached: dict[str, Any] = {}
        self._cached_at = -float("inf")
        self._failures = 0
        self._circuit_until = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self._http.aclose()

    async def snapshot(
        self, query: SnapshotQuery, *, require_fresh: bool = False
    ) -> dict[str, Any]:
        key = query.model_dump_json()
        async with self._lock:
            current = time.monotonic()
            if self._key != key or current - self._cached_at > self._cache_seconds:
                if current < self._circuit_until:
                    raise MarketDataError("Collector circuit is open")
                self._cached = await self._request(query)
                self._key, self._cached_at = key, time.monotonic()
            result = deepcopy(self._cached)
        if require_fresh:
            timestamp = result.get("exchange_timestamp")
            observed = datetime.fromisoformat(timestamp) if timestamp else None
            age = (datetime.now(UTC) - observed).total_seconds() if observed else float("inf")
            if (
                result.get("data_quality_status") != "HEALTHY"
                or not result.get("is_fresh")
                or not 0 <= age <= query.max_age_seconds
            ):
                raise MarketDataError(
                    "Market snapshot did not pass freshness and quality validation"
                )
        return result

    async def _request(self, query: SnapshotQuery) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = await self._http.get(
                    "/api/v2/market/snapshot", params=query.model_dump(exclude_none=True)
                )
                if response.status_code == 429:
                    # Do not retry into a rate limit window.
                    self._circuit_until = time.monotonic() + 60
                    raise MarketDataError("Collector rate limited the request")
                if 400 <= response.status_code < 500:
                    raise MarketDataError(
                        f"Collector rejected the request ({response.status_code})"
                    )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or result.get("schema_version") != "2.0":
                    raise MarketDataError("Unsupported collector response schema")
                if result.get("data_quality_status") == "API_UNAVAILABLE":
                    raise httpx.TransportError("Market storage unavailable")
                self._failures = 0
                return result
            except (httpx.HTTPError, ValueError):
                if attempt < 2:
                    await asyncio.sleep(0.25 * 2**attempt)
        self._failures += 1
        if self._failures >= 3:
            self._circuit_until = time.monotonic() + 30
        raise MarketDataError("Collector request failed after retries")
