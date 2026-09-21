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

CREATE TABLE IF NOT EXISTS backfill_status (
    ticker TEXT PRIMARY KEY,
    completed_through DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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

    def backfill_completed_through(self, ticker: str):
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT completed_through FROM backfill_status WHERE ticker=%s",
                    (ticker,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    def mark_backfill_completed(self, ticker: str, completed_through) -> None:
        with psycopg.connect(self.url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO backfill_status (ticker, completed_through, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (ticker)
                    DO UPDATE SET
                        completed_through = EXCLUDED.completed_through,
                        updated_at = NOW()
                    """,
                    (ticker, completed_through),
                )
            conn.commit()

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
        interval: str = "1m",
    ) -> list[dict[str, Any]]:
        if interval not in INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")
        max_points = max(100, min(max_points, 10000))

        conditions = ["ticker=%s", "timeframe=%s"]
        base_params: list[Any] = [ticker, timeframe]
        if start:
            conditions.append("ts >= %s")
            base_params.append(start)
        if end:
            conditions.append("ts <= %s")
            base_params.append(end)
        where = " AND ".join(conditions)

        if interval == "1m":
            base_sql = f"""
                SELECT ts, open, high, low, close, volume, source
                FROM candles
                WHERE {where}
            """
            params = base_params
        else:
            bucket = INTERVALS[interval]
            base_sql = f"""
                SELECT
                    date_bin(%s::interval, ts, TIMESTAMPTZ '2000-01-01') AS ts,
                    (array_agg(open ORDER BY ts ASC))[1] AS open,
                    MAX(high) AS high,
                    MIN(low) AS low,
                    (array_agg(close ORDER BY ts DESC))[1] AS close,
                    SUM(COALESCE(volume, 0)) AS volume,
                    CASE
                        WHEN bool_and(source='finam') THEN 'finam'
                        WHEN bool_and(source='moex') THEN 'moex'
                        ELSE 'mixed'
                    END AS source
                FROM candles
                WHERE {where}
                GROUP BY 1
            """
            params = [bucket, *base_params]

        sql = f"""
        WITH base AS (
            {base_sql}
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (ORDER BY ts) AS rn,
                   COUNT(*) OVER () AS total
            FROM base
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
