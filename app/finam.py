from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx


class FinamClient:
    BASE_URL = "https://api.finam.ru"

    def __init__(self, secret: str):
        if not secret:
            raise RuntimeError("FINAM_SECRET is empty")
        self.secret = secret
        self.token: str | None = None
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self.client.aclose()

    async def auth(self) -> str:
        response = await self.client.post(
            f"{self.BASE_URL}/v1/sessions",
            json={"secret": self.secret},
        )
        response.raise_for_status()
        self.token = response.json()["token"]
        return self.token

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.token:
            await self.auth()

        response = await self.client.get(
            f"{self.BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        if response.status_code == 401:
            await self.auth()
            response = await self.client.get(
                f"{self.BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        response.raise_for_status()
        return response.json()

    async def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
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
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
