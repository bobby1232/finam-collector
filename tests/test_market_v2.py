"""Market v2 contract tests. All injected market data here are synthetic fixtures."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import market_api
from app.db import Database
from app.market_calendar import session_context
from app.market_ingest import OrderBook, trade_rows
from app.market_metrics import flow_metrics
from app.market_store import MarketStore

TOKEN = "only-used-in-local-test-01234567890"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_orderbook_uses_authoritative_snapshot_then_deltas():
    book = OrderBook()
    bid = {"price": "100", "buy_size": "7"}
    ask = {"price": "101", "sell_size": "3"}
    book.update([dict(bid, action="ACTION_ADD")])
    assert not book.ready
    book.update([bid, ask], snapshot=True)
    book.update([dict(bid, buy_size="8", action="ACTION_UPDATE")])
    assert book.top()["bid"] == [(100, 8)]
    with pytest.raises(ValueError):
        book.update([dict(bid, action="UNKNOWN")])
    assert book.top()["bid"] == [(100, 8)]
    book.update([dict(bid, buy_size="0", action="ACTION_REMOVE")])
    assert book.top()["bid"] == []
    book.update([bid], snapshot=True)
    assert book.top()["ask"] == []


def test_flow_unknown_stays_unknown():
    ts = datetime(2026, 11, 24, 9, tzinfo=UTC)
    rows = trade_rows(
        [{"trade_id": "SYNTH", "price": "100", "size": "3", "timestamp": ts.isoformat()}], ts
    )
    item = flow_metrics(rows, 15, ts + timedelta(seconds=15))[0]
    assert item["unknown_volume"] == 3
    assert item["volume_delta"] is None
    assert item["cvd_observed_window"] is None


def test_calendar_holiday_break_and_missing_date():
    ts = datetime(2026, 11, 24, 9, tzinfo=UTC)
    calendar = {"RTSX": {"2026-11-24": [(ts, ts + timedelta(hours=1))], "2026-11-25": []}}
    assert session_context("SiZ6@RTSX", ts, calendar)["status"] == "OPEN"
    assert (
        session_context("SiZ6@RTSX", ts + timedelta(hours=1), calendar)["status"] == "MARKET_CLOSED"
    )
    assert (
        session_context("SiZ6@RTSX", ts + timedelta(days=1), calendar)["status"] == "MARKET_CLOSED"
    )
    assert session_context("SiZ6@RTSX", ts + timedelta(days=2), calendar)["status"] == "UNKNOWN"


@pytest.fixture
def db_url():
    base = os.getenv("FINAM_TEST_PG_URL")
    if not base:
        pytest.skip("Set FINAM_TEST_PG_URL to an isolated PostgreSQL database")
    schema = f"test_market_{uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
        yield psycopg.conninfo.make_conninfo(base, options=f"-c search_path={schema}")
        conn.execute(
            psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
        )


def test_postgres_migration_idempotency_and_rest_contract(db_url, monkeypatch):
    monkeypatch.setattr(market_api, "DATABASE_URL", db_url)
    monkeypatch.setenv("RESEARCH_API_TOKEN", TOKEN)
    db = Database(db_url)
    db.init_schema()
    store = MarketStore(db_url)
    store.migrate()
    store.migrate()
    now = datetime.now(UTC) - timedelta(seconds=5)
    symbol = "SiZ6@RTSX"
    book = OrderBook()
    book.update(
        [
            {"price": "100", "buy_size": "5", "timestamp": now.isoformat()},
            {"price": "101", "sell_size": "3", "timestamp": now.isoformat()},
        ],
        snapshot=True,
    )
    store.save_book(symbol, book, now)
    store.save_book(symbol, book, now)
    store.save_quote(symbol, now, now, {"last": Decimal("100"), "open_interest": Decimal("200")})
    rows = trade_rows(
        [
            {
                "trade_id": "SYNTH-1",
                "price": "100",
                "size": "100",
                "side": "SIDE_BUY",
                "timestamp": now.isoformat(),
            },
            {
                "trade_id": "SYNTH-2",
                "price": "101",
                "size": "4",
                "side": "SIDE_SELL",
                "timestamp": now.isoformat(),
            },
        ],
        now,
    )
    assert store.save_trades(symbol, rows) == {"written": 2, "duplicates": 0}
    assert store.save_trades(symbol, rows) == {"written": 0, "duplicates": 2}
    app = FastAPI()
    app.state.market_active_symbol = symbol
    app.include_router(market_api.router)
    client = TestClient(app)
    assert client.get("/api/v2/market/orderbook").status_code == 401
    assert (
        client.get("/api/v2/market/orderbook?orderbook_depth=17", headers=AUTH).status_code == 422
    )
    params = {"symbol": symbol, "orderbook_depth": "5", "timeframes": ["1m", "30m"]}
    snapshot = client.get("/api/v2/market/snapshot", params=params, headers=AUTH)
    assert snapshot.status_code == 200, snapshot.text
    result = snapshot.json()
    assert result["schema_version"] == "2.0"
    assert result["data"]["orderbook"]["data"]["microprice"] == 100.625
    assert result["data"]["open_interest"]["data"]["open_interest"] == 200
    assert result["data"]["volume_delta"]["data"]["latest"] == 96
    trades_path = "/api/v2/market/trades"
    first = client.get(trades_path, params={"limit": 1}, headers=AUTH).json()["data"]
    second = client.get(
        trades_path, params={"limit": 1, "cursor": first["next_cursor"]}, headers=AUTH
    ).json()["data"]
    assert first["rows"][0]["trade_id"] != second["rows"][0]["trade_id"]
    assert second["next_cursor"] is None
    large = client.get(f"{trades_path}/large?limit=1", headers=AUTH).json()["data"]
    assert large["rows"][0]["trade_id"] == "SYNTH-1"
    history = client.get("/api/v2/market/orderbook/history", headers=AUTH).json()["data"]
    assert len(history["rows"]) == 1
    unavailable = client.get("/api/v2/market/indicators", headers=AUTH).json()
    assert unavailable["data_quality_status"] == "UNAVAILABLE"
    assert unavailable["data"] is None


def test_research_excludes_events_with_overlapping_windows_and_gaps():
    from app.market_research import research

    ts = datetime(2026, 11, 24, 9, tzinfo=UTC)
    bars = []
    for i in range(700):
        offset = i + (3 if i >= 400 else 0)
        close = 100_000 - i % 30 * 5 if i % 60 >= 30 else 100_000 + i % 30 * 5
        bars.append(
            {
                "exchange_ts": ts + timedelta(minutes=offset),
                "close": close,
                "high": close + 2,
                "low": close - 2,
                "volume": 100,
            }
        )
    data = research(bars, ["rsi_lt_30"], 60)
    for horizon, result in data["horizons"].items():
        count = result["independent_windows"] + result["baseline_windows"]
        assert count * (horizon + 1) <= len(bars)
    assert data["horizons"][1]["independent_windows"] > 0
    assert data["horizons"][50]["independent_windows"] <= data["horizons"][1]["independent_windows"]
    missing = research(bars, ["book_imbalance"], 60)
    assert missing["eligible_feature_rows"] == 0


async def test_stream_reconnects_and_requests_fresh_snapshot(monkeypatch):
    import asyncio
    import json
    from contextlib import asynccontextmanager

    import app.market_ingest as module

    connections = []

    class Finam:
        async def auth(self):
            return "fixture"

    class Socket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

        async def recv(self):
            return '{"event_info":{"event":"HANDSHAKE_SUCCESS"}}'

    @asynccontextmanager
    async def connect(*args, **kwargs):
        ws = Socket()
        connections.append(ws)
        yield ws

    rest_calls, statuses = [], []

    class Collector(module.MarketIngest):
        async def rest(self, feed):
            rest_calls.append(feed)

        async def health(self, feed, status, **detail):
            statuses.append(status)

        async def consume(self, ws, book):
            assert not book.ready
            if len(connections) == 1:
                book.update([], snapshot=True)
                raise ConnectionError
            raise asyncio.CancelledError

    async def sleep(_delay):
        return None

    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await Collector(None, Finam(), "SiZ6@RTSX", 5, 30, 15).stream()
    assert rest_calls == ["book", "trades", "book", "trades"]
    assert statuses == ["PARTIAL_DATA", "STREAM_DISCONNECTED", "PARTIAL_DATA"]
    assert [s["type"] for s in connections[-1].sent] == ["ORDER_BOOK", "INSTRUMENT_TRADES"]


def test_batched_flow_load(db_url):
    import time

    from app.market_store import load_flow

    store = MarketStore(db_url)
    store.migrate()
    symbol = "SiZ6@RTSX"
    now = datetime.now(UTC)
    start = now.replace(second=0, microsecond=0) - timedelta(hours=1)
    rows = [
        {
            "trade_id": f"SYNTH-LOAD-{i}",
            "price": Decimal(100 + i % 400),
            "quantity": Decimal(2),
            "side": "BUY" if i % 2 else "SELL",
            "side_method": "FINAM_REPORTED_SIDE",
            "oi": None,
            "received_ts": now,
            "exchange_ts": start + timedelta(milliseconds=i * 100),
        }
        for i in range(10_000)
    ]
    started = time.monotonic()
    for i in range(0, len(rows), 500):
        assert store.save_trades(symbol, rows[i : i + 500])["written"] == 500
    ingest_time = time.monotonic() - started
    started = time.monotonic()
    with store.reader() as (conn, cutoff):
        flow = load_flow(conn, symbol, start, cutoff, 60)
        assert sum(row["trades_count"] for row in flow) == len(rows)
        assert sum(row["buy_volume"] - row["sell_volume"] for row in flow) == 0
        size = conn.execute(
            "SELECT sum(pg_total_relation_size(relid)) AS bytes FROM pg_statio_user_tables "
            "WHERE schemaname=current_schema()"
        ).fetchone()["bytes"]
    elapsed_read = time.monotonic() - started
    print(
        f"10,000 synthetic trades: ingest {ingest_time:.3f}s, read {elapsed_read:.3f}s,"
        f" relation bytes {size}"
    )
