import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("finam-collector.finam")


class FinamClient:
    BASE_URL = "https://api.finam.ru"

    def __init__(
        self,
        secret: str,
        *,
        max_retries: int = 5,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 15.0,
        max_requests_per_second: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not secret:
            raise RuntimeError("FINAM_SECRET is empty")
        self.secret = secret
        self.token: str | None = None
        self.client = httpx.AsyncClient(timeout=30.0, transport=transport)
        self.max_retries = max(0, max_retries)
        self.backoff_base_seconds = max(0.0, backoff_base_seconds)
        self.backoff_max_seconds = max(0.0, backoff_max_seconds)
        self.min_request_interval = 1.0 / max(0.1, max_requests_per_second)
        self._request_lock = asyncio.Lock()
        self._last_request_started = 0.0
        self.stats = {
            "max_requests_per_second": max_requests_per_second,
            "retry_count": 0,
            "http_429_count": 0,
            "last_429": None,
            "last_retry_delay_seconds": None,
        }

    async def close(self):
        await self.client.aclose()

    async def auth(self) -> str:
        response = await self._request_with_retry(
            "POST",
            "/v1/sessions",
            json={"secret": self.secret},
        )
        response.raise_for_status()
        self.token = response.json()["token"]
        return self.token

    async def _throttle(self) -> None:
        async with self._request_lock:
            now = time.monotonic()
            delay = self.min_request_interval - (now - self._last_request_started)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_started = time.monotonic()

    def _retry_delay(self, response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(self.backoff_max_seconds, max(0.0, float(retry_after)))
                except ValueError:
                    pass
        exponential = min(
            self.backoff_max_seconds,
            self.backoff_base_seconds * (2**attempt),
        )
        return exponential * secrets.SystemRandom().uniform(0.8, 1.2)

    async def _request_with_retry(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            response = None
            try:
                await self._throttle()
                response = await self.client.request(
                    method,
                    f"{self.BASE_URL}{path}",
                    **kwargs,
                )
                if response.status_code != 429 and response.status_code < 500:
                    if response.status_code != 401:
                        response.raise_for_status()
                    return response
                if response.status_code == 429:
                    self.stats["http_429_count"] += 1
                    self.stats["last_429"] = datetime.now(UTC).isoformat()
                last_error = httpx.HTTPStatusError(
                    f"Finam transient HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except httpx.RequestError as exc:
                last_error = exc

            if attempt == self.max_retries:
                assert last_error is not None
                raise last_error

            delay = self._retry_delay(response, attempt)
            self.stats["retry_count"] += 1
            self.stats["last_retry_delay_seconds"] = round(delay, 3)
            log.warning(
                "Retrying Finam request path=%s attempt=%s status=%s delay=%.3fs",
                path,
                attempt + 1,
                response.status_code if response is not None else "network_error",
                delay,
            )
            await asyncio.sleep(delay)

        raise RuntimeError("unreachable")

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.token:
            await self.auth()

        response = await self._request_with_retry(
            "GET",
            path,
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 401:
            await self.auth()
            response = await self._request_with_retry(
                "GET",
                path,
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        response.raise_for_status()
        return response.json()

    async def bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        symbol_path = quote(symbol, safe="@")
        data = await self._get(
            f"/v1/instruments/{symbol_path}/bars",
            {
                "timeframe": timeframe,
                "interval.start_time": self._iso(start),
                "interval.end_time": self._iso(end),
            },
        )
        return data.get("bars", [])

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
