import asyncio
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

MOSCOW = ZoneInfo("Europe/Moscow")

MOEX_INSTRUMENTS = {
    "GAZP@MISX": {
        "url": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/GAZP/candles.json",
        "secid": "GAZP",
    },
    "ROSN@MISX": {
        "url": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/ROSN/candles.json",
        "secid": "ROSN",
    },
    "SBER@MISX": {
        "url": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER/candles.json",
        "secid": "SBER",
    },
    "GMKN@MISX": {
        "url": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/GMKN/candles.json",
        "secid": "GMKN",
    },
    "SiZ6@RTSX": {
        "url": "https://iss.moex.com/iss/engines/futures/markets/forts/securities/SiZ6/candles.json",
        "secid": "SiZ6",
    },
}

MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


def _contract_secid(prefix: str, year: int, month: int) -> str:
    return f"{prefix}{MONTH_CODES[month]}{year % 10}"


def _next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def brent_candidate_secids(day: date) -> tuple[str, str]:
    current = _contract_secid("BR", day.year, day.month)
    next_year, next_month = _next_month(day.year, day.month)
    following = _contract_secid("BR", next_year, next_month)
    return current, following


def si_candidate_secids(day: date) -> tuple[str, str]:
    quarter_months = (3, 6, 9, 12)
    current_month = next(m for m in quarter_months if m >= day.month)
    current_year = day.year

    if current_month == 12:
        next_year, next_q_month = current_year + 1, 3
    else:
        next_year, next_q_month = current_year, current_month + 3

    current = _contract_secid("Si", current_year, current_month)
    following = _contract_secid("Si", next_year, next_q_month)
    return current, following


class MoexClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "finam-collector/1.0 historical-backfill"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _fetch(
        self,
        url: str,
        symbol: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        start = 0

        while True:
            response = None
            last_error: Exception | None = None
            for attempt in range(5):
                try:
                    response = await self.client.get(
                        url,
                        params={
                            "from": date_from.isoformat(),
                            "till": date_to.isoformat(),
                            "interval": 1,
                            "start": start,
                            "iss.meta": "off",
                            "iss.only": "candles",
                        },
                    )
                    if response.status_code == 404:
                        return []
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.HTTPStatusError(
                            f"MOEX transient HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    last_error = None
                    break
                except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                    last_error = exc
                    if attempt == 4:
                        raise
                    await asyncio.sleep(min(5.0, 0.5 * (2 ** attempt)))

            if response is None:
                raise RuntimeError(f"MOEX request failed for {symbol}") from last_error

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

    @staticmethod
    def _volume_score(bars: list[dict[str, Any]]) -> float:
        score = 0.0
        for bar in bars:
            try:
                score += float(bar.get("volume") or 0)
            except (TypeError, ValueError):
                pass
        return score

    async def _continuous_day(
        self,
        symbol: str,
        day: date,
        candidates: tuple[str, str],
    ) -> list[dict[str, Any]]:
        best_bars: list[dict[str, Any]] = []
        best_score = -1.0

        for secid in candidates:
            url = (
                "https://iss.moex.com/iss/engines/futures/markets/forts/"
                f"securities/{secid}/candles.json"
            )
            bars = await self._fetch(url, f"{symbol}/{secid}", day, day)
            score = self._volume_score(bars)
            if bars and score > best_score:
                best_bars = bars
                best_score = score

        return best_bars

    async def minute_bars(
        self,
        symbol: str,
        date_from: date,
        date_to: date | None = None,
    ) -> list[dict[str, Any]]:
        date_to = date_to or date_from

        if symbol == "BR@CONT":
            if date_to != date_from:
                raise RuntimeError("BR@CONT backfill expects one calendar day per request")
            return await self._continuous_day(
                symbol,
                date_from,
                brent_candidate_secids(date_from),
            )

        if symbol == "SI@CONT":
            if date_to != date_from:
                raise RuntimeError("SI@CONT backfill expects one calendar day per request")
            return await self._continuous_day(
                symbol,
                date_from,
                si_candidate_secids(date_from),
            )

        cfg = MOEX_INSTRUMENTS.get(symbol)
        if not cfg:
            raise RuntimeError(f"No MOEX backfill mapping for {symbol}")

        return await self._fetch(cfg["url"], symbol, date_from, date_to)
