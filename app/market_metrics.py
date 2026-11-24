from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from math import log, sqrt
from statistics import correlation, mean, pstdev
from zoneinfo import ZoneInfo

from .analysis import atr, ema, ema_series, rsi

MOSCOW = ZoneInfo("Europe/Moscow")


def book_metrics(book, depth):
    bids = sorted(book["bid"], key=lambda x: x["price"], reverse=True)
    asks = sorted(book["ask"], key=lambda x: x["price"])
    bid, ask = bids[:depth], asks[:depth]
    b, a = sum(x["quantity"] for x in bid), sum(x["quantity"] for x in ask)
    best_bid = bid[0]["price"] if bid else None
    best_ask = ask[0]["price"] if ask else None
    valid = best_bid is not None and best_ask is not None and best_bid < best_ask
    spread = best_ask - best_bid if valid else None
    mid = (best_ask + best_bid) / 2 if valid else None
    microprice = (
        (
            (best_ask * bid[0]["quantity"] + best_bid * ask[0]["quantity"])
            / (bid[0]["quantity"] + ask[0]["quantity"])
        )
        if valid
        else None
    )
    imbalance = {}
    for width in (5, 10, 20):
        bv = sum(x["quantity"] for x in bids[:width])
        av = sum(x["quantity"] for x in asks[:width])
        imbalance[width] = (bv - av) / (bv + av) if len(bids) >= width <= len(asks) else None
    return dict(
        book,
        bid=bid,
        ask=ask,
        best_bid=best_bid,
        best_ask=best_ask,
        spread=spread,
        mid_price=mid,
        spread_bps=10000 * spread / abs(mid) if mid else None,
        bid_volume=b,
        ask_volume=a,
        bid_ask_ratio=b / a if a else None,
        imbalance=(b - a) / (b + a) if valid else None,
        imbalance_by_depth=imbalance,
        weighted_mid_price=(b * best_ask + a * best_bid) / (b + a) if valid else None,
        microprice=microprice,
        depth={"bid": len(bid), "ask": len(ask)},
        requested_depth=depth,
        stored_depth={"bid": len(bids), "ask": len(asks)},
    )


def liquidity(current, previous):
    def levels(book):
        return {(s, row["price"]): row["quantity"] for s in ("bid", "ask") for row in book[s]}

    mid = current["mid_price"]
    now = levels(current)
    old = levels(previous) if previous else {}
    walls = {}
    for side in ("bid", "ask"):
        rows = current[side]
        total = sum(r["quantity"] for r in rows)
        walls[side] = [
            dict(
                row,
                distance_from_mid=abs(row["price"] - mid) if mid is not None else None,
                depth_share=row["quantity"] / total,
                also_in_previous=(side, row["price"]) in old if previous else None,
            )
            for row in sorted(rows, key=lambda r: r["quantity"], reverse=True)[:3]
        ]
    shared = now.keys() & old.keys()
    changes = [now[key] - old[key] for key in shared]
    return {
        "largest_visible_levels": walls,
        "added_at_shared_prices": sum(x for x in changes if x > 0) if previous else None,
        "removed_at_shared_prices": -sum(x for x in changes if x < 0) if previous else None,
        "disappeared_visible_levels": len(old.keys() - now.keys()) if previous else None,
        "method": "Observed depth changes can include fills, cancellations and rank changes.",
    }


def flow_metrics(rows, interval, as_of):
    buckets = defaultdict(list)
    for row in rows:
        seconds = int(row["exchange_ts"].timestamp())
        buckets[seconds // interval * interval].append(row)
    aggregated = []
    for start, trades in sorted(buckets.items()):
        volumes = Counter()
        for trade in trades:
            volumes[trade["side"]] += trade["quantity"]
        aggregated.append(
            {
                "bucket_timestamp": datetime.fromtimestamp(start, UTC),
                "buy_volume": volumes["BUY"],
                "sell_volume": volumes["SELL"],
                "unknown_volume": volumes["UNKNOWN"],
                "trades_count": len(trades),
            }
        )
    return flow_buckets(aggregated, interval, as_of)


def flow_buckets(buckets, interval, as_of):
    cvd = 0
    result = []
    previous = None
    stride = timedelta(seconds=interval)
    for bucket in buckets:
        buy, sell, unknown = (bucket[k] for k in ("buy_volume", "sell_volume", "unknown_volume"))
        known = buy + sell
        total = known + unknown
        delta = buy - sell if known else None
        if delta is not None:
            cvd += delta
        ts = bucket["bucket_timestamp"]
        complete = ts + stride <= as_of
        count = bucket["trades_count"]
        result.append(
            {
                **bucket,
                "complete_by_clock": complete,
                "volume_delta": delta,
                "cvd_observed_window": cvd if delta is not None else None,
                "trades_per_second": count / interval if complete else None,
                "average_trade_size": total / count,
                "side_imbalance": delta / known if known else None,
                "volume_acceleration_ratio": (
                    total / previous[1] - 1
                    if previous and ts - previous[0] == stride and complete
                    else None
                ),
            }
        )
        previous = (ts, total)
    return result


def volume_profile(rows):
    volumes = Counter()
    for trade in rows:
        volumes[trade["price"]] += trade["quantity"]
    prices = sorted(volumes)
    poc_index = max(range(len(prices)), key=lambda i: volumes[prices[i]])
    total = sum(volumes.values())
    left = right = poc_index
    in_area = volumes[prices[poc_index]]
    while 10 * in_area < 7 * total:
        low = volumes[prices[left - 1]] if left else -1
        high = volumes[prices[right + 1]] if right + 1 < len(prices) else -1
        if low >= high:
            left -= 1
            in_area += low
        else:
            right += 1
            in_area += high
    return {
        "poc": prices[poc_index],
        "val": prices[left],
        "vah": prices[right],
        "value_area_fraction": in_area / total,
        "total_traded_volume": total,
        "distinct_traded_prices": len(prices),
        "largest_price_bins": [
            {"price": price, "volume": vol} for price, vol in volumes.most_common(50)
        ],
    }


def oi_metrics(rows, price_context=None):
    current = rows[0]
    # A quote and a trade can update independently. Compare a single source.
    same = [r for r in rows if r["source"] == current["source"]]
    values = [r["open_interest"] for r in same[1:]]
    out = {}
    for minutes in (1, 5, 15, 30, 60):
        cutoff = current["exchange_ts"] - timedelta(minutes=minutes)
        previous = next((r for r in same if r["exchange_ts"] <= cutoff), None)
        if previous and cutoff - previous["exchange_ts"] > timedelta(minutes=minutes):
            previous = None
        base = previous["open_interest"] if previous else None
        change = current["open_interest"] - base if base is not None else None
        out[str(minutes)] = {
            "absolute": change,
            "percentage": 100 * change / base if base else None,
            "baseline_timestamp": previous["exchange_ts"] if previous else None,
        }
    value = current["open_interest"]
    sd = pstdev(values) if len(values) >= 2 else None
    observed = (current["exchange_ts"] - same[1]["exchange_ts"]).total_seconds() if values else None
    return dict(
        current,
        changes_minutes=out,
        previous_sample_interval_seconds=observed,
        historical_percentile=percentile(value, values),
        z_score=float(value - mean(values)) / float(sd) if sd else None,
        price_context=price_context,
    )


def percentile(value, history):
    if value is None or not history:
        return None
    return (
        100
        * (sum(x < value for x in history) + sum(x == value for x in history) / 2)
        / len(history)
    )


def directional_indicators(bars, period=14):
    ups, downs, ranges = [], [], []
    for prev, cur in zip(bars[:-1], bars[1:], strict=True):
        up, down = cur["high"] - prev["high"], prev["low"] - cur["low"]
        ups.append(up if up > max(down, 0) else 0)
        downs.append(down if down > max(up, 0) else 0)
        ranges.append(
            max(
                cur["high"] - cur["low"],
                abs(cur["high"] - prev["close"]),
                abs(cur["low"] - prev["close"]),
            )
        )
    if len(ranges) < 2 * period - 1:
        return None
    u, d, tr = (sum(values[:period]) for values in (ups, downs, ranges))
    dx = []
    plus = minus = None
    for i in range(period - 1, len(ranges)):
        if i >= period:
            u, d, tr = (
                u - u / period + ups[i],
                d - d / period + downs[i],
                tr - tr / period + ranges[i],
            )
        plus, minus = (100 * u / tr, 100 * d / tr) if tr else (0, 0)
        dx.append(100 * abs(plus - minus) / (plus + minus) if plus + minus else 0)
    adx = mean(dx[:period])
    for value in dx[period:]:
        adx += (value - adx) / period
    return {"adx": adx, "plus_di": plus, "minus_di": minus}


def candle_vwap(bars):
    if not bars or any(b["volume"] is None for b in bars):
        return None
    volume = sum(b["volume"] for b in bars)
    if volume <= 0:
        return None
    pairs = [((b["high"] + b["low"] + b["close"]) / 3, b["volume"]) for b in bars]
    center = sum(p * v for p, v in pairs) / volume
    dev = sqrt(sum((p - center) ** 2 * v for p, v in pairs) / volume)
    return {
        "vwap": center,
        "stddev": dev,
        "upper_1": center + dev,
        "lower_1": center - dev,
        "upper_2": center + 2 * dev,
        "lower_2": center - 2 * dev,
        "method": "OHLCV typical-price approximation",
        "anchor": bars[0]["exchange_ts"],
    }


def indicators(bars, session_bars=(), previous_day=(), opening_bars=()):
    close = [b["close"] for b in bars]
    last = close[-1]
    mean20 = mean(close[-20:]) if len(close) >= 20 else None
    sd20 = pstdev(close[-20:]) if mean20 is not None else None
    fast, slow = ema_series(close, 12), ema_series(close, 26)
    macd = [x - y for x, y in zip(fast, slow, strict=True) if x is not None and y is not None]
    signal = ema(macd, 9)
    rsi14 = [rsi(close[:i], 14) for i in range(max(1, len(close) - 13), len(close) + 1)]
    rsi14 = [r for r in rsi14 if r is not None]
    stoch = (
        ((rsi14[-1] - min(rsi14)) / (max(rsi14) - min(rsi14)))
        if (len(rsi14) == 14 and max(rsi14) != min(rsi14))
        else None
    )
    returns = (
        [log(b / a) for a, b in zip(close[-21:-1], close[-20:], strict=True) if a > 0 and b > 0]
        if (len(close) >= 21)
        else []
    )
    volumes = [b["volume"] for b in bars]
    obv = None
    if all(v is not None for v in volumes):
        obv = sum(
            (b > a) * v - (b < a) * v
            for a, b, v in zip(close[:-1], close[1:], volumes[1:], strict=True)
        )
    mfi = None
    tail = bars[-15:]
    if len(tail) == 15 and all(b["volume"] is not None for b in tail):
        typical = [(b["high"] + b["low"] + b["close"]) / 3 for b in tail]
        money = [
            ((b > a) - (b < a), b * row["volume"])
            for a, b, row in zip(typical[:-1], typical[1:], tail[1:], strict=True)
        ]
        positive = sum(v for side, v in money if side > 0)
        negative = sum(v for side, v in money if side < 0)
        if positive + negative > 0:
            mfi = 100 * positive / (positive + negative)
    anchored = candle_vwap(bars)
    session = candle_vwap(session_bars)
    previous = high_low(previous_day)
    pivots = None
    if previous:
        pivot = (previous["high"] + previous["low"] + previous_day[-1]["close"]) / 3
        pivots = {
            "pivot": pivot,
            "r1": 2 * pivot - previous["low"],
            "s1": 2 * pivot - previous["high"],
        }
    levels = high_low(bars[-21:-1]) if len(bars) >= 21 else None
    rel_average = (
        mean(volumes[-21:-1])
        if len(volumes) >= 21 and all(v is not None for v in volumes[-21:-1])
        else None
    )
    return {
        "exchange_ts": bars[-1]["exchange_ts"],
        "candles_used": len(bars),
        "provisional": False,
        "close": last,
        "rsi": {p: rsi(close, p) for p in (7, 14, 21)},
        "ema": {p: ema(close, p) for p in (9, 20, 50, 100, 200)},
        "sma": {p: mean(close[-p:]) if len(close) >= p else None for p in (20, 50, 200)},
        "atr14": atr(bars, 14),
        "directional_indicators": directional_indicators(bars),
        "macd": {
            "line": macd[-1] if macd else None,
            "signal": signal,
            "histogram": macd[-1] - signal if signal is not None else None,
        },
        "bollinger": {"center": mean20, "upper": mean20 + 2 * sd20, "lower": mean20 - 2 * sd20}
        if sd20 is not None
        else None,
        "stochastic_rsi": stoch,
        "roc_12_pct": 100 * (last / close[-13] - 1) if len(close) > 12 and close[-13] else None,
        "momentum_10": last - close[-11] if len(close) > 10 else None,
        "obv_from_window_start": obv,
        "mfi14": mfi,
        "session_vwap": session,
        "anchored_vwap": anchored,
        "distance_from_vwap": last - session["vwap"] if session else None,
        "realized_volatility_20_unannualized": sqrt(sum(r * r for r in returns))
        if len(returns) == 20
        else None,
        "relative_volume_20": volumes[-1] / rel_average
        if rel_average and volumes[-1] is not None
        else None,
        "support_resistance_20": levels,
        "distance_from_support": last - levels["low"] if levels else None,
        "distance_from_resistance": levels["high"] - last if levels else None,
        "session_high_low": high_low(session_bars),
        "previous_day_high_low": previous,
        "opening_range_15m": high_low(opening_bars),
        "pivot_points": pivots,
    }


def high_low(bars):
    return (
        {"high": max(b["high"] for b in bars), "low": min(b["low"] for b in bars)} if bars else None
    )


def cross_market(left, right, seconds):
    lmap = {row["exchange_ts"]: row["close"] for row in left}
    rmap = {row["exchange_ts"]: row["close"] for row in right}
    step = timedelta(seconds=seconds)

    def returns(prices):
        return {
            t: prices[t] / prices[t - step] - 1
            for t in prices
            if t - step in prices and prices[t - step] > 0
        }

    lr, rr = returns(lmap), returns(rmap)
    same = sorted(lr.keys() & rr.keys())
    if not same:
        return None

    def corr(pairs):
        if len(pairs) < 20:
            return None
        a, b = zip(*pairs, strict=True)
        return correlation(a, b) if max(a) != min(a) and max(b) != min(b) else None

    lead_lag = {}
    for lag in range(-3, 4):
        pairs = [(lr[t], rr[t + lag * step]) for t in lr if t + lag * step in rr]
        lead_lag[lag] = {"correlation": corr(pairs), "pairs": len(pairs)}
    last = same[-1]
    return {
        "timestamp": last,
        "matched_returns": len(same),
        "left_close": lmap[last],
        "right_close": rmap[last],
        "latest_return_divergence": lr[last] - rr[last],
        "rolling_correlation_60": corr([(lr[t], rr[t]) for t in same[-60:]]),
        "lead_lag": lead_lag,
        "lag_semantics": "Positive lag compares left now with right later.",
    }
