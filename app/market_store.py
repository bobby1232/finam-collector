from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from marketdata.contracts import FRAME_SECONDS

SOURCE = "finam"
SCHEMA_FILE = Path(__file__).with_name("migrations") / "0001_market_data.sql"
FLOW_WINDOW_LIMIT = 250_000


def execute(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


class MarketStore:
    def __init__(self, url):
        self.url = url

    def migrate(self):
        with psycopg.connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(695671234)")
            conn.execute(SCHEMA_FILE.read_text())

    @contextmanager
    def write(self, symbol):
        with psycopg.connect(self.url) as conn:
            conn.execute(
                "INSERT INTO market_instruments (symbol, source) VALUES (%s,%s)"
                " ON CONFLICT DO NOTHING",
                (symbol, SOURCE),
            )
            yield conn

    @contextmanager
    def reader(self):
        with psycopg.connect(self.url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '4s'")
            # Pin the database snapshot before taking the application cutoff.
            conn.execute("SELECT 1 FROM pg_catalog.pg_class LIMIT 1")
            yield conn, datetime.now(UTC)

    def save_book(self, symbol, book, received):
        levels = book.top()
        bids, asks = levels["bid"], levels["ask"]
        quality = "HEALTHY" if bids and asks and bids[0][0] < asks[0][0] else "PARTIAL_DATA"
        digest = hashlib.sha256(repr((book.exchange_ts, levels)).encode()).hexdigest()
        with self.write(symbol) as conn:
            row = conn.execute(
                """INSERT INTO orderbook_snapshots
                   (symbol, source, exchange_ts, received_ts, fingerprint, quality)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                (symbol, SOURCE, book.exchange_ts, received, digest, quality),
            ).fetchone()
            if row is None:
                return
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO orderbook_levels (snapshot_id,side,price,quantity)"
                    " VALUES (%s,%s,%s,%s)",
                    [
                        (row[0], side, price, qty)
                        for side, data in levels.items()
                        for price, qty in data
                    ],
                )

    @staticmethod
    def save_oi(conn, symbol, ts, received, value, source):
        conn.execute(
            """INSERT INTO open_interest_history
               (symbol,source,exchange_ts,received_ts,open_interest) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            (symbol, source, ts, received, value),
        )

    def save_quote(self, symbol, ts, received, values):
        with self.write(symbol) as conn:
            conn.execute(
                """INSERT INTO market_quotes (symbol,source,exchange_ts,received_ts,quote)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    symbol,
                    SOURCE,
                    ts,
                    received,
                    Jsonb({key: str(val) for key, val in values.items()}),
                ),
            )
            if values.get("open_interest") is not None:
                self.save_oi(conn, symbol, ts, received, values["open_interest"], "finam_quote")

    def save_trades(self, symbol, rows):
        written = 0
        if not rows:
            return {"written": 0, "duplicates": 0}
        with self.write(symbol) as conn, conn.cursor() as cur:
            payload = [
                {
                    key: str(value)
                    if key in {"price", "quantity", "exchange_ts", "received_ts"}
                    else value
                    for key, value in row.items()
                    if key != "oi"
                }
                for row in rows
            ]
            written = cur.execute(
                """WITH inserted AS (
                    INSERT INTO market_trades
                      (symbol,source,trade_id,exchange_ts,received_ts,price,quantity,side,side_method)
                    SELECT %s,%s,trade_id,exchange_ts,received_ts,price,quantity,side,side_method
                    FROM jsonb_to_recordset(%s) AS t(
                        trade_id text,exchange_ts timestamptz,received_ts timestamptz,
                        price numeric,quantity numeric,side text,side_method text
                    )
                    ORDER BY trade_id ON CONFLICT DO NOTHING
                    RETURNING exchange_ts, quantity, side
                ), grouped AS (
                    SELECT step, date_bin(make_interval(secs=>step), exchange_ts,
                                          timestamptz '1970-01-01 00:00+00') AS bucket,
                           max(exchange_ts) AS latest,
                           coalesce(sum(quantity) FILTER (WHERE side='BUY'),0) AS buy,
                           coalesce(sum(quantity) FILTER (WHERE side='SELL'),0) AS sell,
                           coalesce(sum(quantity) FILTER (WHERE side='UNKNOWN'),0) AS unknown,
                           count(*) AS n
                    FROM inserted CROSS JOIN (VALUES (15),(60)) intervals(step)
                    GROUP BY step,bucket
                ), aggregated AS (
                    INSERT INTO market_flow_aggregates AS a
                      (symbol,interval_seconds,bucket_ts,latest_trade_ts,buy_volume,sell_volume,
                       unknown_volume,trades_count)
                    SELECT %s,step,bucket,latest,buy,sell,unknown,n FROM grouped
                    ORDER BY step,bucket
                    ON CONFLICT (symbol,interval_seconds,bucket_ts) DO UPDATE SET
                      latest_trade_ts=greatest(a.latest_trade_ts,excluded.latest_trade_ts),
                      buy_volume=a.buy_volume+excluded.buy_volume,
                      sell_volume=a.sell_volume+excluded.sell_volume,
                      unknown_volume=a.unknown_volume+excluded.unknown_volume,
                      trades_count=a.trades_count+excluded.trades_count
                    RETURNING 1
                )
                SELECT count(*) FROM inserted""",
                (symbol, SOURCE, Jsonb(payload), symbol),
            ).fetchone()[0]
            for row in rows:
                if row["oi"] is not None:
                    self.save_oi(
                        conn,
                        symbol,
                        row["exchange_ts"],
                        row["received_ts"],
                        row["oi"],
                        "finam_trade",
                    )
        return {"written": written, "duplicates": len(rows) - written}

    def health(self, symbol, feed, status, detail):
        with self.write(symbol) as conn:
            conn.execute(
                """INSERT INTO ingestion_health (symbol,feed,status,updated_at,detail)
                   VALUES (%s,%s,%s,now(),%s)
                   ON CONFLICT (symbol,feed) DO UPDATE
                   SET status=excluded.status, updated_at=excluded.updated_at,
                       detail=excluded.detail""",
                (symbol, feed, status, Jsonb(detail)),
            )


def load_book(conn, symbol, start, as_of, limit=1, after=None):
    books = execute(
        conn,
        """SELECT id, source, exchange_ts, received_ts, sequence_id, quality
                 FROM orderbook_snapshots WHERE symbol=%s AND received_ts BETWEEN %s AND %s
                   AND (exchange_ts IS NULL OR exchange_ts<=%s)
                   AND (received_ts,id)<(%s,%s)
                 ORDER BY received_ts DESC,id DESC LIMIT %s""",
        (symbol, start, as_of, as_of, *(after or (as_of, 2**63 - 1)), limit),
    )
    if books:
        levels = execute(
            conn,
            """SELECT snapshot_id,side,price,quantity,orders_count
                                 FROM orderbook_levels WHERE snapshot_id=ANY(%s)""",
            ([book["id"] for book in books],),
        )
        by_id = {book["id"]: book for book in books}
        for book in books:
            book.update(bid=[], ask=[])
        for level in levels:
            book = by_id[level.pop("snapshot_id")]
            book[level.pop("side")].append(level)
    return books


def load_trades(
    conn, symbol, start, as_of, limit=FLOW_WINDOW_LIMIT + 1, after=None, *, min_quantity=0
):
    return execute(
        conn,
        """SELECT source,trade_id,exchange_ts,received_ts,price,quantity,side,side_method
                 FROM market_trades WHERE symbol=%s AND exchange_ts BETWEEN %s AND %s
                   AND received_ts<=%s AND (exchange_ts,trade_id)<(%s,%s) AND quantity >= %s
                 ORDER BY exchange_ts DESC,trade_id DESC LIMIT %s""",
        (symbol, start, as_of, as_of, *(after or (as_of, "\uffff")), min_quantity, limit),
    )


def load_oi(conn, symbol, start, as_of, limit, after=None):
    return execute(
        conn,
        """SELECT source,exchange_ts,received_ts,open_interest FROM open_interest_history
                 WHERE symbol=%s AND exchange_ts BETWEEN %s AND %s AND received_ts<=%s
                   AND (exchange_ts,source)<(%s,%s)
                 ORDER BY exchange_ts DESC,source DESC LIMIT %s""",
        (symbol, start, as_of, as_of, *(after or (as_of, "z")), limit),
    )


def load_quote(conn, symbol, as_of):
    return execute(
        conn,
        """SELECT source,exchange_ts,received_ts,quote FROM market_quotes
                 WHERE symbol=%s AND exchange_ts<=%s AND received_ts<=%s
                 ORDER BY exchange_ts DESC LIMIT 1""",
        (symbol, as_of, as_of),
    )


def load_bars(conn, symbol, frame, as_of, *, limit=500, window=None, complete=True):
    seconds = FRAME_SECONDS[frame]
    end = (
        as_of
        if not complete
        else datetime.fromtimestamp(int(as_of.timestamp()) // seconds * seconds, UTC)
    )
    start = as_of - timedelta(seconds=window or min(365 * 86400, seconds * limit * 4))
    bars = execute(
        conn,
        """WITH buckets AS (
                   SELECT date_bin(%s::interval, ts, timestamptz '1970-01-01 00:00+00') AS bucket,
                          ts,open,high,low,close,volume,source,updated_at
                   FROM candles WHERE ticker=%s AND timeframe='TIME_FRAME_M1'
                     AND ts >= %s AND ts < %s
                 )
                 SELECT bucket AS exchange_ts,
                        (array_agg(open ORDER BY ts))[1] AS open,
                        max(high) AS high,min(low) AS low,
                        (array_agg(close ORDER BY ts DESC))[1] AS close,
                        CASE WHEN count(volume)=count(*) THEN sum(volume) END AS volume,
                        CASE WHEN min(source)=max(source) THEN min(source)
                             ELSE 'mixed' END AS source,
                        max(updated_at) AS received_ts
                 FROM buckets GROUP BY bucket HAVING bucket >= %s
                 ORDER BY bucket DESC LIMIT %s""",
        (f"{seconds} seconds", symbol, start, end, start, limit),
    )
    for bar in bars:
        for key in ("open", "high", "low", "close", "volume"):
            if bar[key] is not None:
                bar[key] = float(bar[key])
    return list(reversed(bars))


def load_market_features(conn, symbol, frame, start, end, *, need_book, need_oi, need_cvd):
    seconds = FRAME_SECONDS[frame]
    stride = f"{seconds} seconds"
    data = {}
    # Each input must have been received before the corresponding feature candle closed.
    # Trade and book samples are observations with unknown tape/depth completeness.
    if need_cvd:
        trades = execute(
            conn,
            """WITH t AS (
                       SELECT date_bin(%s::interval,exchange_ts,
                                       timestamptz '1970-01-01 00:00+00') AS ts,
                              received_ts,quantity,side
                       FROM market_trades WHERE symbol=%s AND exchange_ts >= %s AND exchange_ts < %s
                     )
                     SELECT ts, sum(CASE WHEN side='BUY' THEN quantity ELSE -quantity END)
                            FILTER (WHERE side IN ('BUY','SELL')) AS delta
                     FROM t WHERE received_ts < ts + %s::interval GROUP BY ts""",
            (stride, symbol, start, end, stride),
        )
        data["volume_delta"] = {
            r["ts"]: float(r["delta"]) for r in trades if r["delta"] is not None
        }
    if need_oi:
        oi = execute(
            conn,
            """WITH t AS (
                       SELECT date_bin(%s::interval,exchange_ts,
                                       timestamptz '1970-01-01 00:00+00') AS ts,
                              exchange_ts,received_ts,open_interest
                       FROM open_interest_history WHERE symbol=%s AND source='finam_quote'
                         AND exchange_ts >= %s AND exchange_ts < %s
                     )
                     SELECT DISTINCT ON (ts) ts, open_interest FROM t
                     WHERE received_ts < ts + %s::interval ORDER BY ts,exchange_ts DESC""",
            (stride, symbol, start, end, stride),
        )
        data["oi"] = {r["ts"]: float(r["open_interest"]) for r in oi}
    if need_book:
        books = execute(
            conn,
            """WITH t AS (
                       SELECT date_bin(%s::interval,received_ts,
                                       timestamptz '1970-01-01 00:00+00') AS ts,
                              id,received_ts
                       FROM orderbook_snapshots WHERE symbol=%s AND quality='HEALTHY'
                         AND exchange_ts BETWEEN received_ts - interval '30 seconds' AND received_ts
                         AND received_ts >= %s AND received_ts < %s
                     ), latest AS (
                       SELECT DISTINCT ON (ts) ts,id FROM t ORDER BY ts,received_ts DESC,id DESC
                     ), volumes AS (
                       SELECT ts, sum(quantity) FILTER (WHERE side='bid') AS bid,
                              sum(quantity) FILTER (WHERE side='ask') AS ask
                       FROM latest JOIN orderbook_levels ON snapshot_id=id GROUP BY ts
                     )
                     SELECT ts,(bid-ask)/NULLIF(bid+ask,0) AS imbalance FROM volumes""",
            (stride, symbol, start, end),
        )
        data["book_imbalance"] = {
            r["ts"]: float(r["imbalance"]) for r in books if r["imbalance"] is not None
        }
    return data


def load_flow(conn, symbol, start, as_of, interval):
    stride = f"{interval} seconds"
    base = 60 if interval % 60 == 0 else 15
    return execute(
        conn,
        """SELECT date_bin(%s::interval,bucket_ts,timestamptz '1970-01-01 00:00+00')
                        AS bucket_timestamp,
                        max(latest_trade_ts) AS exchange_ts,
                        sum(buy_volume) AS buy_volume,sum(sell_volume) AS sell_volume,
                        sum(unknown_volume) AS unknown_volume,
                        sum(trades_count)::bigint AS trades_count
                 FROM market_flow_aggregates
                 WHERE symbol=%s AND interval_seconds=%s AND bucket_ts >= %s
                   AND latest_trade_ts <= %s
                 GROUP BY 1 ORDER BY 1""",
        (stride, symbol, base, start, as_of),
    )


def load_latest_minute(conn, symbol, as_of):
    return execute(
        conn,
        """SELECT ts AS exchange_ts,updated_at AS received_ts,source,close
                 FROM candles WHERE ticker=%s AND timeframe='TIME_FRAME_M1' AND ts <= %s
                 ORDER BY ts DESC LIMIT 1""",
        (symbol, as_of),
    )
