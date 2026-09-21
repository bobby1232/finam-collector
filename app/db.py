from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candles (
    ticker       TEXT NOT NULL,
    timeframe    TEXT NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    open         NUMERIC(24,10) NOT NULL,
    high         NUMERIC(24,10) NOT NULL,
    low          NUMERIC(24,10) NOT NULL,
    close        NUMERIC(24,10) NOT NULL,
    volume       NUMERIC(30,10),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, timeframe, ts)
);

CREATE INDEX IF NOT EXISTS idx_candles_ticker_tf_ts
ON candles (ticker, timeframe, ts DESC);
"""

UPSERT_SQL = """
INSERT INTO candles (ticker, timeframe, ts, open, high, low, close, volume, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (ticker, timeframe, ts)
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    updated_at = NOW();
"""


def _num(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("value")
    return Decimal(str(value))


class Database:
    def __init__(self, url: str):
        if not url:
            raise RuntimeError("DATABASE_URL is empty")
        self.url = url

    def init_schema(self) -> None:
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def upsert_bars(self, ticker: str, timeframe: str, bars: list[dict[str, Any]]) -> int:
        if not bars:
            return 0
        rows = []
        for bar in bars:
            ts = datetime.fromisoformat(bar["timestamp"].replace("Z", "+00:00"))
            rows.append((
                ticker,
                timeframe,
                ts,
                _num(bar["open"]),
                _num(bar["high"]),
                _num(bar["low"]),
                _num(bar["close"]),
                _num(bar["volume"]) if bar.get("volume") is not None else None,
            ))
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.executemany(UPSERT_SQL, rows)
            conn.commit()
        return len(rows)

    def latest_timestamp(self, ticker: str, timeframe: str):
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(ts) FROM candles WHERE ticker=%s AND timeframe=%s",
                    (ticker, timeframe),
                )
                return cur.fetchone()[0]

    def count(self, ticker: str, timeframe: str) -> int:
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM candles WHERE ticker=%s AND timeframe=%s",
                    (ticker, timeframe),
                )
                return cur.fetchone()[0]
