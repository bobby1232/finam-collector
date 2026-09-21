import os

TICKER = os.getenv("TICKER", "GAZP@MISX").strip().upper()
TIMEFRAME = os.getenv("TIMEFRAME", "TIME_FRAME_M1").strip().upper()
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

if TIMEFRAME not in TIMEFRAME_SECONDS:
    raise RuntimeError(f"Unsupported TIMEFRAME: {TIMEFRAME}")

# Poll no faster than 15 sec; for long candles this remains reasonably lightweight.
POLL_SECONDS = int(os.getenv("POLL_SECONDS", str(max(15, min(TIMEFRAME_SECONDS[TIMEFRAME] // 2, 300)))))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", str(HISTORY_DAYS[TIMEFRAME])))
