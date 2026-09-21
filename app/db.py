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
    source       TEXT NOT NULL DEFAULT 'finam',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (ticker, timeframe, ts)
);

ALTER TABLE candles
ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'finam';

CREATE INDEX IF NOT EXISTS idx_candles_ticker_tf_ts
ON candles (ticker, timeframe, ts DESC);
"""

UPSERT_SQL = """
INSERT INTO candles
    (ticker, timeframe, ts, open, high, low, close, volume, source, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
ON CONFLICT (ticker, timeframe, ts)
DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    volume = EXCLUDED.volume,
    source = EXCLUDED.source,
    updated_at = NOW();
"""

INTERVALS = {
    "1m": None,
    "5m": "5 minutes",
    "15m": "15 minutes",
    "1h": "1 hour",
    "4h": "4 hours",
    "1d": "1 day",
}


def _num(value: Any) -> Decimal:
    if isinstance(value, dict):
        value = value.get("value")
    return Decimal(str(value))


def _json_num(value):
    return None if value is None else float(value)


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

    def upsert_bars(
        self,
        ticker: str,
        timeframe: str,
        bars: list[dict[str, Any]],
        source: str = "finam",
    ) -> int:
        if not bars:
            return 0
        rows = []
        for bar in bars:
            ts = datetime.fromisoformat(bar["timestamp"].replace("Z", "+00:00"))
            rows.append(
                (
                    ticker,
                    timeframe,
                    ts,
                    _num(bar["open"]),
                    _num(bar["high"]),
                    _num(bar["low"]),
                    _num(bar["close"]),
                    _num(bar["volume"]) if bar.get("volume") is not None else None,
                    source,
                )
            )
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

    def earliest_timestamp(self, ticker: str, timeframe: str):
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MIN(ts) FROM candles WHERE ticker=%s AND timeframe=%s",
                    (ticker, timeframe),
                )
                return cur.fetchone()[0]

    def stats(self, ticker: str, timeframe: str) -> dict[str, Any]:
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS rows,
                        MIN(ts) AS earliest_timestamp,
                        MAX(ts) AS latest_timestamp,
                        COUNT(volume) AS rows_with_volume,
                        COUNT(*) FILTER (WHERE volume > 0) AS rows_with_positive_volume,
                        (array_agg(close ORDER BY ts DESC))[1] AS latest_close,
                        (array_agg(volume ORDER BY ts DESC))[1] AS latest_volume
                    FROM candles
                    WHERE ticker=%s AND timeframe=%s
                    """,
                    (ticker, timeframe),
                )
                row = cur.fetchone()

                cur.execute(
                    """
                    SELECT source, COUNT(*)
                    FROM candles
                    WHERE ticker=%s AND timeframe=%s
                    GROUP BY source
                    ORDER BY source
                    """,
                    (ticker, timeframe),
                )
                sources = {r[0]: r[1] for r in cur.fetchall()}

                return {
                    "rows": row[0],
                    "earliest_timestamp": row[1],
                    "latest_timestamp": row[2],
                    "rows_with_volume": row[3],
                    "rows_with_positive_volume": row[4],
                    "latest_close": row[5],
                    "latest_volume": row[6],
                    "sources": sources,
                }

    def recent_bars(
        self, ticker: str, timeframe: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ts, open, high, low, close, volume, source, updated_at
                    FROM candles
                    WHERE ticker=%s AND timeframe=%s
                    ORDER BY ts DESC
                    LIMIT %s
                    """,
                    (ticker, timeframe, limit),
                )
                return [
                    {
                        "timestamp": r[0],
                        "open": r[1],
                        "high": r[2],
                        "low": r[3],
                        "close": r[4],
                        "volume": r[5],
                        "source": r[6],
                        "updated_at": r[7],
                    }
                    for r in cur.fetchall()
                ]

    def chart_bars(
        self,
        ticker: str,
        interval: str,
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, Any]]:
        if interval not in INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")

        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                if interval == "1m":
                    cur.execute(
                        """
                        SELECT ts, open, high, low, close, volume
                        FROM candles
                        WHERE ticker=%s
                          AND timeframe='TIME_FRAME_M1'
                          AND ts >= %s AND ts <= %s
                        ORDER BY ts
                        """,
                        (ticker, date_from, date_to),
                    )
                else:
                    bucket = INTERVALS[interval]
                    cur.execute(
                        """
                        SELECT
                            date_bin(%s::interval, ts, TIMESTAMPTZ '2000-01-01') AS bucket,
                            (array_agg(open ORDER BY ts ASC))[1] AS open,
                            MAX(high) AS high,
                            MIN(low) AS low,
                            (array_agg(close ORDER BY ts DESC))[1] AS close,
                            SUM(COALESCE(volume, 0)) AS volume
                        FROM candles
                        WHERE ticker=%s
                          AND timeframe='TIME_FRAME_M1'
                          AND ts >= %s AND ts <= %s
                        GROUP BY bucket
                        ORDER BY bucket
                        """,
                        (bucket, ticker, date_from, date_to),
                    )

                result = []
                for r in cur.fetchall():
                    result.append(
                        {
                            "time": int(r[0].timestamp()),
                            "open": _json_num(r[1]),
                            "high": _json_num(r[2]),
                            "low": _json_num(r[3]),
                            "close": _json_num(r[4]),
                            "volume": _json_num(r[5]),
                        }
                    )
                return result
