import os
from dataclasses import dataclass

FINAM_SECRET = os.getenv("FINAM_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

TIMEFRAME_SECONDS = {
    "TIME_FRAME_M1": 60,
    "TIME_FRAME_M5": 300,
    "TIME_FRAME_M15": 900,
    "TIME_FRAME_M30": 1800,
    "TIME_FRAME_H1": 3600,
    "TIME_FRAME_H2": 7200,
    "TIME_FRAME_H4": 14400,
    "TIME_FRAME_H8": 28800,
    "TIME_FRAME_D": 86400,
    "TIME_FRAME_W": 604800,
    "TIME_FRAME_MN": 2592000,
    "TIME_FRAME_QR": 7776000,
}

HISTORY_DAYS = {
    "TIME_FRAME_M1": 7,
    "TIME_FRAME_M5": 30,
    "TIME_FRAME_M15": 30,
    "TIME_FRAME_M30": 30,
    "TIME_FRAME_H1": 30,
    "TIME_FRAME_H2": 30,
    "TIME_FRAME_H4": 30,
    "TIME_FRAME_H8": 30,
    "TIME_FRAME_D": 365,
    "TIME_FRAME_W": 365 * 5,
    "TIME_FRAME_MN": 365 * 5,
    "TIME_FRAME_QR": 365 * 5,
}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    timeframe: str

    @property
    def history_days(self) -> int:
        return HISTORY_DAYS[self.timeframe]


def parse_instruments() -> list[Instrument]:
    """
    INSTRUMENTS format:
      GAZP@MISX:TIME_FRAME_M1,SiZ6@RTSX:TIME_FRAME_M1

    Symbol case is preserved intentionally: futures tickers in Finam may be mixed-case.
    Falls back to legacy TICKER/TIMEFRAME variables when INSTRUMENTS is absent.
    """
    raw = os.getenv("INSTRUMENTS", "").strip()

    if not raw:
        ticker = os.getenv("TICKER", "GAZP@MISX").strip()
        timeframe = os.getenv("TIMEFRAME", "TIME_FRAME_M1").strip().upper()
        raw = f"{ticker}:{timeframe}"

    result: list[Instrument] = []
    seen: set[tuple[str, str]] = set()

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            symbol, timeframe = item.rsplit(":", 1)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid INSTRUMENTS item: {item!r}. Expected SYMBOL:TIMEFRAME"
            ) from exc

        symbol = symbol.strip()
        timeframe = timeframe.strip().upper()

        if not symbol or "@" not in symbol:
            raise RuntimeError(f"Invalid Finam symbol: {symbol!r}; expected ticker@mic")
        if timeframe not in TIMEFRAME_SECONDS:
            raise RuntimeError(f"Unsupported TIMEFRAME: {timeframe}")

        key = (symbol, timeframe)
        if key not in seen:
            seen.add(key)
            result.append(Instrument(symbol=symbol, timeframe=timeframe))

    if not result:
        raise RuntimeError("INSTRUMENTS is empty")

    return result


INSTRUMENTS = parse_instruments()

# How often the worker checks Finam. Current/unfinished candle is refreshed via UPSERT.
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "30")))
