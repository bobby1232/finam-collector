from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .analytics import INTERVAL_SECONDS, normalize_bar
from .config import Settings

ALLOWED_SYMBOLS = {"SiZ6@RTSX", "SI@CONT"}
ALLOWED_INTERVALS = set(INTERVAL_SECONDS)


class CollectorError(RuntimeError):
    pass


class CollectorClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.base_url = str(settings.collector_base_url).rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.get(path, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise CollectorError("Collector returned a non-object response")
            return payload
        except (httpx.HTTPError, ValueError) as exc:
            raise CollectorError(f"Collector request failed for {path}") from exc

    @staticmethod
    def validate_symbol(symbol: str) -> str:
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Unsupported symbol. Allowed: {', '.join(sorted(ALLOWED_SYMBOLS))}")
        return symbol

    @staticmethod
    def validate_interval(interval: str) -> str:
        if interval not in ALLOWED_INTERVALS:
            raise ValueError(
                f"Unsupported timeframe. Allowed: {', '.join(sorted(ALLOWED_INTERVALS))}"
            )
        return interval

    async def bars(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        symbol = self.validate_symbol(symbol)
        interval = self.validate_interval(interval)
        limit = max(1, min(limit, 500))
        if interval == "1m":
            payload = await self._get(
                "/bars",
                params={"symbol": symbol, "timeframe": "TIME_FRAME_M1", "limit": limit},
            )
        else:
            seconds = INTERVAL_SECONDS[interval]
            now = datetime.now(UTC)
            # Include weekends/session gaps. max_points stays at 10k so the collector does not
            # stride-sample the series: indicators require contiguous interval buckets.
            start = now - timedelta(seconds=seconds * (limit * 3 + 20), days=7)
            payload = await self._get(
                "/api/candles",
                params={
                    "symbol": symbol,
                    "timeframe": "TIME_FRAME_M1",
                    "start": start.isoformat(),
                    "end": now.isoformat(),
                    "max_points": 10000,
                    "interval": interval,
                },
            )
        bars = payload.get("bars", [])
        if not isinstance(bars, list):
            raise CollectorError("Collector bars field is invalid")
        normalized = sorted((normalize_bar(x) for x in bars), key=lambda x: x["timestamp"])
        return normalized[-limit:]

    async def health(self) -> dict[str, Any]:
        return await self._get("/health")

    async def oversold_research(
        self, symbol: str, timeframe: str, thresholds: str, horizons: str
    ) -> dict[str, Any]:
        token = self.settings.research_api_token.get_secret_value()
        if len(token) < 32:
            raise CollectorError("Research API token is not configured")
        if timeframe not in {"15m", "1h", "1d"}:
            raise ValueError("Research timeframe must be 15m, 1h, or 1d")
        return await self._get(
            "/api/research/si/oversold",
            params={
                "symbol": self.validate_symbol(symbol),
                "timeframe": timeframe,
                "thresholds": thresholds,
                "horizons": horizons,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
