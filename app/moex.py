import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

MOSCOW = ZoneInfo("Europe/Moscow")

MOEX_INSTRUMENTS = {
    "GAZP@MISX": {
        "url": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/GAZP/candles.json",
        "secid": "GAZP",
    },
    "SiZ6@RTSX": {
        "url": "https://iss.moex.com/iss/engines/futures/markets/forts/securities/SiZ6/candles.json",
        "secid": "SiZ6",
    },
}


class MoexClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "finam-collector/1.0 historical-backfill"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def minute_bars(
        self,
        symbol: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        cfg = MOEX_INSTRUMENTS.get(symbol)
        if not cfg:
            raise RuntimeError(f"No MOEX backfill mapping for {symbol}")

        result: list[dict[str, Any]] = []
        start = 0

        while True:
            response = await self.client.get(
                cfg["url"],
                params={
                    "from": date_from.isoformat(),
                    "till": date_to.isoformat(),
                    "interval": 1,
                    "start": start,
                    "iss.meta": "off",
                    "iss.only": "candles",
                },
            )
            response.raise_for_status()
            payload = response.json().get("candles", {})
            columns = payload.get("columns", [])
            data = payload.get("data", [])
            if not data:
                break

            index = {name: i for i, name in enumerate(columns)}
            required = {"begin", "open", "high", "low", "close", "volume"}
            if not required.issubset(index):
                raise RuntimeError(
                    f"Unexpected MOEX candle columns for {symbol}: {columns}"
                )

            for row in data:
                begin = datetime.fromisoformat(str(row[index["begin"]]))
                if begin.tzinfo is None:
                    begin = begin.replace(tzinfo=MOSCOW)
                begin_utc = begin.astimezone(timezone.utc)

                result.append(
                    {
                        "timestamp": begin_utc.isoformat().replace("+00:00", "Z"),
                        "open": row[index["open"]],
                        "high": row[index["high"]],
                        "low": row[index["low"]],
                        "close": row[index["close"]],
                        "volume": row[index["volume"]],
                    }
                )

            start += len(data)
            if len(data) < 500:
                break
            await asyncio.sleep(0.03)

        return result

    async def range_minute_bars(
        self,
        symbol: str,
        date_from: date,
        date_to: date,
        chunk_days: int = 7,
    ):
        cursor = date_from
        while cursor <= date_to:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), date_to)
            bars = await self.minute_bars(symbol, cursor, chunk_end)
            yield cursor, chunk_end, bars
            cursor = chunk_end + timedelta(days=1)
