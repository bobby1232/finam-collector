from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect

log = logging.getLogger(__name__)
FINAM_WS = "wss://api.finam.ru/ws"
SUBSCRIPTIONS = ("ORDER_BOOK", "INSTRUMENT_TRADES")
# The provider calls this 'trade side'. Its aggressor interpretation is unverified.
TRADE_SIDES = {"SIDE_BUY": "BUY", "SIDE_SELL": "SELL", 1: "BUY", 2: "SELL"}


def timestamp(value):
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("Provider timestamp has no offset")
    return ts.astimezone(UTC)


def number(value):
    if isinstance(value, dict):
        value = value["value"]
    val = Decimal(str(value))
    if not val.is_finite() or len(val.as_tuple().digits) > 30 or abs(val.adjusted()) > 30:
        raise ValueError("Invalid decimal")
    return val


def trade_rows(rows, received):
    result = []
    for row in rows:
        trade_id = row["trade_id"]
        if not isinstance(trade_id, str) or not 0 < len(trade_id) <= 128:
            raise ValueError("Missing exchange trade ID")
        price, size = number(row["price"]), number(row["size"])
        if size <= 0:
            raise ValueError("Invalid trade size")
        side = TRADE_SIDES.get(row.get("side"))
        result.append(
            {
                "trade_id": trade_id,
                "exchange_ts": timestamp(row["timestamp"]),
                "received_ts": received,
                "price": price,
                "quantity": size,
                "side": side or "UNKNOWN",
                "side_method": "FINAM_REPORTED_SIDE" if side else "UNKNOWN",
                "oi": number(row["open_interest"])
                if row.get("open_interest") is not None
                else None,
            }
        )
    counts = Counter(item["exchange_ts"] for item in result)
    previous = None
    for item in sorted(result, key=lambda trade: trade["exchange_ts"]):
        if counts[item["exchange_ts"]] != 1:
            previous = None
            continue
        if item["side"] == "UNKNOWN" and previous is not None and item["price"] != previous:
            item["side"] = "BUY" if item["price"] > previous else "SELL"
            item["side_method"] = "TICK_RULE"
        previous = item["price"]
    return result


def quote_row(row):
    fields = ("last", "last_size", "bid", "ask", "bid_size", "ask_size", "volume", "open_interest")
    return timestamp(row["timestamp"]), {
        name: number(row[name]) for name in fields if row.get(name) is not None
    }


class OrderBook:
    def __init__(self):
        self.levels = {}
        self.ready = False
        self.exchange_ts = None

    def update(self, rows, *, snapshot=False):
        if not snapshot and not self.ready:
            return
        updated = {} if snapshot else self.levels.copy()
        new_times = []
        for row in rows:
            fields = [field for field in ("buy_size", "sell_size") if field in row]
            if len(fields) != 1:
                raise ValueError("Unknown order book side")
            field = fields[0]
            side = "bid" if field == "buy_size" else "ask"
            price, quantity = number(row["price"]), number(row[field])
            key = (side, price, row.get("mpid") or "")
            if quantity < 0:
                raise ValueError("Invalid book quantity")
            action = row.get("action")
            if action in ("ACTION_REMOVE", 1) or quantity == 0:
                updated.pop(key, None)
            elif action in ("ACTION_ADD", "ACTION_UPDATE", 2, 3) or (
                snapshot and action in (None, "ACTION_UNSPECIFIED", 0)
            ):
                updated[key] = quantity
            else:
                raise ValueError("Unknown order book action")
            if row.get("timestamp"):
                new_times.append(timestamp(row["timestamp"]))
        self.levels = updated
        self.ready = True
        previous = self.exchange_ts if not snapshot else None
        self.exchange_ts = max(new_times + ([previous] if previous else []), default=None)

    def top(self, depth=50):
        grouped = defaultdict(Decimal)
        for (side, price, _), qty in self.levels.items():
            grouped[side, price] += qty
        return {
            side: sorted(
                ((price, qty) for (s, price), qty in grouped.items() if s == side),
                reverse=side == "bid",
            )[:depth]
            for side in ("bid", "ask")
        }


def quality_for_error(exc):
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return "RATE_LIMITED"
    return "API_UNAVAILABLE"


class MarketIngest:
    def __init__(self, db, finam, symbol, book_seconds, trade_seconds, quote_seconds):
        self.db = db
        self.finam = finam
        self.symbol = symbol
        self.path = f"/v1/instruments/{quote(symbol, safe='@')}"
        self.book_seconds = book_seconds
        self.trade_seconds = trade_seconds
        self.quote_seconds = quote_seconds
        self.reconnects = 0

    async def rest(self, feed):
        paths = {"book": "/orderbook", "trades": "/trades/latest", "quote": "/quotes/latest"}
        payload = await self.finam._get(self.path + paths[feed])
        received = datetime.now(UTC)
        if payload.get("symbol") not in (None, self.symbol):
            raise ValueError("Provider symbol mismatch")
        if feed == "book":
            book = OrderBook()
            book.update(payload["orderbook"]["rows"], snapshot=True)
            await asyncio.to_thread(self.db.save_book, self.symbol, book, received)
        elif feed == "trades":
            rows = trade_rows(payload["trades"], received)
            return await asyncio.to_thread(self.db.save_trades, self.symbol, rows)
        else:
            ts, values = quote_row(payload["quote"])
            await asyncio.to_thread(self.db.save_quote, self.symbol, ts, received, values)
        return None

    async def health(self, feed, status, **detail):
        await asyncio.to_thread(self.db.health, self.symbol, feed, status, detail)

    async def poll(self, feed, interval):
        failures = 0
        while True:
            started = time.monotonic()
            try:
                counts = await self.rest(feed)
                status = "PARTIAL_DATA" if feed == "trades" else "HEALTHY"
                await self.health(
                    feed,
                    status,
                    mode="rest",
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    counts=counts,
                )
                failures = 0
            except Exception as exc:
                failures += 1
                log.warning(
                    "Market data poll failed symbol=%s feed=%s error=%s",
                    self.symbol,
                    feed,
                    type(exc).__name__,
                )
                await self.health(feed, quality_for_error(exc), error_type=type(exc).__name__)
            delay = max(interval, min(300, 2 ** min(failures, 8))) if failures else interval
            await asyncio.sleep(delay * secrets.SystemRandom().uniform(0.9, 1.1))

    async def stream(self):
        failures = 0
        while True:
            connected_at = None
            try:
                token = await self.finam.auth()
                async with connect(
                    FINAM_WS,
                    additional_headers={"Authorization": token},
                    open_timeout=10,
                    max_size=2_097_152,
                ) as ws:
                    async with asyncio.timeout(10):
                        greeting = json.loads(await ws.recv())
                        if greeting.get("event_info", {}).get("event") != "HANDSHAKE_SUCCESS":
                            raise ValueError("Finam handshake failed")
                    for feed in SUBSCRIPTIONS:
                        await ws.send(
                            json.dumps(
                                {
                                    "action": "SUBSCRIBE",
                                    "type": feed,
                                    "data": {"symbol": self.symbol},
                                }
                            )
                        )
                    book = OrderBook()
                    # REST results serve users while waiting for an authoritative stream snapshot.
                    # REST and stream updates cannot be ordered without a common sequence.
                    for feed in ("book", "trades"):
                        try:
                            await self.rest(feed)
                        except Exception as exc:
                            log.warning(
                                "Market data recovery failed feed=%s error=%s",
                                feed,
                                type(exc).__name__,
                            )
                    await self.health(
                        "stream",
                        "PARTIAL_DATA",
                        reconnects=self.reconnects,
                        waiting_for_snapshot=True,
                        missing_events=None,
                    )
                    connected_at = time.monotonic()
                    await self.consume(ws, book)
            except Exception as exc:
                failures += 1
                if connected_at is not None and time.monotonic() - connected_at >= 60:
                    failures = 1
                self.reconnects += 1
                await self.health(
                    "stream",
                    "STREAM_DISCONNECTED",
                    reconnects=self.reconnects,
                    missing_events=None,
                    error_type=type(exc).__name__,
                )
                log.warning(
                    "Market stream disconnected symbol=%s error=%s", self.symbol, type(exc).__name__
                )
                await asyncio.sleep(
                    min(60, 2 ** min(failures, 6)) * secrets.SystemRandom().uniform(0.8, 1.2)
                )

    async def consume(self, ws, book):
        pending = []
        last_book = last_health = last_trades = time.monotonic()
        changed = False
        try:
            while True:
                try:
                    async with asyncio.timeout(1):
                        message = await ws.recv()
                    envelope = json.loads(message)
                except TimeoutError:
                    envelope = {}
                if (
                    envelope.get("type") == "ERROR"
                    or envelope.get("event_info", {}).get("event") == "CONNECTION_CLOSED"
                ):
                    raise ConnectionError("Provider stream error")
                received = datetime.now(UTC)
                if envelope.get("type") == "DATA":
                    payload = envelope["payload"]
                    if payload.get("error"):
                        raise ConnectionError("Provider subscription error")
                    feed = envelope.get("subscription_type")
                    if feed == "INSTRUMENT_TRADES" and payload.get("symbol") == self.symbol:
                        pending.extend(trade_rows(payload["trades"], received))
                    if feed == "ORDER_BOOK":
                        for event in payload["order_book"]:
                            if event.get("symbol") != self.symbol:
                                continue
                            snapshot = event.get("is_data_snapshot") is True
                            book.update(event["rows"], snapshot=snapshot)
                            changed |= book.ready

                now = time.monotonic()
                if pending and (now - last_trades >= 1 or len(pending) >= 512):
                    counts = await asyncio.to_thread(self.db.save_trades, self.symbol, pending)
                    pending = []
                    last_trades = now
                    await self.health(
                        "trades",
                        "PARTIAL_DATA",
                        mode="stream",
                        recovery_completeness="UNVERIFIED",
                        counts=counts,
                    )
                if now - last_book >= self.book_seconds:
                    if changed:
                        await asyncio.to_thread(self.db.save_book, self.symbol, book, received)
                        changed = False
                    if not book.ready:
                        await self.rest("book")
                    last_book = now
                if now - last_health >= 15:
                    if book.ready:
                        await self.health(
                            "book", "HEALTHY", mode="stream", sequence_verification="UNAVAILABLE"
                        )
                    await self.health(
                        "stream",
                        "HEALTHY" if book.ready else "PARTIAL_DATA",
                        reconnects=self.reconnects,
                        missing_events=None,
                        waiting_for_snapshot=not book.ready,
                    )
                    last_health = now
        finally:
            if pending:
                await asyncio.to_thread(self.db.save_trades, self.symbol, pending)

    async def run(self, mode):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.poll("quote", self.quote_seconds))
            if mode == "stream":
                tasks.create_task(self.stream())
            else:
                tasks.create_task(self.poll("book", self.book_seconds))
                tasks.create_task(self.poll("trades", self.trade_seconds))
