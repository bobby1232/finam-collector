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

ALTER TABLE candles ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'finam';

CREATE INDEX IF NOT EXISTS idx_candles_ticker_tf_ts
ON candles (ticker, timeframe, ts DESC);
"""

UPSERT_SQL = """
INSERT INTO candles (ticker, timeframe, ts, open, high, low, close, volume, source, updated_at)
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
            rows.append((
                ticker,
                timeframe,
                ts,
                _num(bar["open"]),
                _num(bar["high"]),
                _num(bar["low"]),
                _num(bar["close"]),
                _num(bar["volume"]) if bar.get("volume") is not None else None,
                source,
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
                        COUNT(*) FILTER (WHERE source='finam') AS finam_rows,
                        COUNT(*) FILTER (WHERE source='moex') AS moex_rows
                    FROM candles
                    WHERE ticker=%s AND timeframe=%s
                    """,
                    (ticker, timeframe),
                )
                row = cur.fetchone()
                return {
                    "rows": row[0],
                    "earliest_timestamp": row[1],
                    "latest_timestamp": row[2],
                    "rows_with_volume": row[3],
                    "rows_with_positive_volume": row[4],
                    "finam_rows": row[5],
                    "moex_rows": row[6],
                }

    def recent_bars(self, ticker: str, timeframe: str, limit: int = 10) -> list[dict[str, Any]]:
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
        timeframe: str,
        start: datetime | None,
        end: datetime | None,
        max_points: int = 5000,
    ) -> list[dict[str, Any]]:
        max_points = max(100, min(max_points, 10000))
        conditions = ["ticker=%s", "timeframe=%s"]
        params: list[Any] = [ticker, timeframe]
        if start:
            conditions.append("ts >= %s")
            params.append(start)
        if end:
            conditions.append("ts <= %s")
            params.append(end)
        where = " AND ".join(conditions)

        sql = f"""
        WITH ranked AS (
            SELECT ts, open, high, low, close, volume, source,
                   ROW_NUMBER() OVER (ORDER BY ts) AS rn,
                   COUNT(*) OVER () AS total
            FROM candles
            WHERE {where}
        ),
        sampled AS (
            SELECT *,
                   GREATEST(1, CEIL(total::numeric / %s)::int) AS stride
            FROM ranked
        )
        SELECT ts, open, high, low, close, volume, source
        FROM sampled
        WHERE MOD(rn - 1, stride) = 0 OR rn = total
        ORDER BY ts
        """
        params.append(max_points)
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return [
                    {
                        "timestamp": r[0],
                        "open": r[1],
                        "high": r[2],
                        "low": r[3],
                        "close": r[4],
                        "volume": r[5],
                        "source": r[6],
                    }
                    for r in cur.fetchall()
                ]
