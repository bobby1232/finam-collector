from datetime import timedelta
from math import erfc, sqrt
from statistics import mean, median, pstdev

from .analysis import rsi_series, wilson
from .market_metrics import percentile

HORIZONS = (1, 3, 5, 10, 20, 50)
TARGETS = (50, 100, 200, 300, 500, 1000)
REQUIRES_MICROSTRUCTURE = {
    "oi_anomaly",
    "price_cvd_divergence",
    "price_oi_divergence",
    "book_imbalance",
}


def relative(value, previous):
    return value / previous - 1 if previous else None


def zscore(value, history):
    if value is None or len(history) < 20:
        return None
    sd = pstdev(history)
    return (value - mean(history)) / sd if sd else None


def features(bars, seconds, observations):
    closes = [b["close"] for b in bars]
    rsis = rsi_series(closes)
    result = []
    step = timedelta(seconds=seconds)
    for i, bar in enumerate(bars):
        ts = bar["exchange_ts"]
        oi = observations.get("oi", {})
        oi_change = oi[ts] - oi[ts - step] if ts in oi and ts - step in oi else None
        delta = observations.get("volume_delta", {}).get(ts)
        recent = bars[max(0, i - 19) : i + 1]
        ranges = [b["high"] - b["low"] for b in recent]
        vwap = None
        if len(recent) == 20 and all(b["volume"] is not None for b in recent):
            volume = sum(b["volume"] for b in recent)
            if volume:
                vwap = (
                    sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in recent)
                    / volume
                )
        result.append(
            {
                "rsi": rsis[i],
                "volume": bar["volume"],
                "price_change": relative(closes[i], closes[i - 1]) if i else None,
                "range_volatility": mean(ranges) if len(ranges) == 20 else None,
                "vwap_deviation": relative(closes[i], vwap),
                "oi_change": oi_change,
                "volume_delta": delta,
                "book_imbalance": observations.get("book_imbalance", {}).get(ts),
            }
        )
    return result


def flags(rows, index):
    row = rows[index]
    history = rows[max(0, index - 100) : index]

    def z(key):
        return zscore(row[key], [r[key] for r in history if r[key] is not None])

    price, vol, vwap = z("price_change"), z("volume"), z("vwap_deviation")
    values = {
        "price_drop": price is not None and price <= -2,
        "price_rise": price is not None and price >= 2,
        "volume_anomaly": vol is not None and vol >= 2,
        "vwap_deviation": vwap is not None and abs(vwap) >= 2,
    }
    oi = z("oi_change")
    values["oi_anomaly"] = oi is not None and abs(oi) >= 2
    change, delta, oi_change = row["price_change"], row["volume_delta"], row["oi_change"]
    values["price_cvd_divergence"] = (
        change * delta < 0 if change is not None and delta is not None else False
    )
    values["price_oi_divergence"] = (
        change * oi_change < 0 if change is not None and oi_change is not None else False
    )
    imbalance = row["book_imbalance"]
    values["book_imbalance"] = imbalance is not None and abs(imbalance) >= 0.6
    value = row["rsi"]
    values.update({f"rsi_lt_{x}": value is not None and value < x for x in (30, 25, 20)})
    values.update({f"rsi_gt_{x}": value is not None and value > x for x in (70, 75, 80)})
    return values


def distribution(values):
    ordered = sorted(values)
    return (
        {
            "mean": mean(ordered),
            "median": median(ordered),
            "quantiles": {
                f"p{p}": ordered[round((len(ordered) - 1) * p / 100)]
                for p in (0, 10, 25, 50, 75, 90, 100)
            },
        }
        if ordered
        else None
    )


def probability(successes, n):
    return {"frequency": successes / n, "wilson_95": wilson(successes, n)} if n else None


def outcomes(bars, entries, horizon):
    result = []
    for i in entries:
        base = bars[i]["close"]
        future = bars[i + 1 : i + 1 + horizon]
        result.append(
            (
                future[-1]["close"] - base,
                max(0, max(b["high"] - base for b in future)),
                max(0, max(base - b["low"] for b in future)),
            )
        )
    return result


def test_positive(a, b):
    if min(len(a), len(b)) < 30:
        return None
    pa, pb = sum(x[0] > 0 for x in a) / len(a), sum(x[0] > 0 for x in b) / len(b)
    pooled = (pa * len(a) + pb * len(b)) / (len(a) + len(b))
    sd = sqrt(pooled * (1 - pooled) * (1 / len(a) + 1 / len(b)))
    return erfc(abs(pa - pb) / (sd * sqrt(2))) if sd else None


def research(bars, conditions, seconds, observations=None):
    rows = features(bars, seconds, observations or {})
    feature_flags = (flags(rows, i) for i in range(len(rows)))
    matches = [all(state[c] for c in conditions) for state in feature_flags]
    fields = {
        "oi_anomaly": "oi_change",
        "price_oi_divergence": "oi_change",
        "price_cvd_divergence": "volume_delta",
        "book_imbalance": "book_imbalance",
    }
    required = [fields[condition] for condition in conditions if condition in fields]
    eligible = [all(row[field] is not None for field in required) for row in rows]
    crossings = {
        i
        for i in range(1, len(matches))
        if eligible[i] and eligible[i - 1] and matches[i] and not matches[i - 1]
    }
    horizons = {}
    for horizon in HORIZONS:
        events, baseline = [], []
        available = len(bars) - horizon
        # Admit only fully contiguous outcome windows. Keep baseline and event windows disjoint.
        step = timedelta(seconds=seconds)
        valid = [
            i
            for i in range(100, available)
            if all(
                bars[j]["exchange_ts"] - bars[j - 1]["exchange_ts"] == step
                for j in range(i + 1, i + horizon + 1)
            )
        ]
        for i in valid:
            if i in crossings and (not events or i > events[-1] + horizon):
                events.append(i)
        occupied = {i for event in events for i in range(event, event + horizon + 1)}
        for i in valid:
            if (
                eligible[i]
                and not matches[i]
                and (not baseline or i > baseline[-1] + horizon)
                and not any(j in occupied for j in range(i, i + horizon + 1))
            ):
                baseline.append(i)
        sample = outcomes(bars, events, horizon)
        control = outcomes(bars, baseline, horizon)
        n = len(sample)
        p = test_positive(sample, control)
        horizons[horizon] = {
            "independent_windows": n,
            "baseline_windows": len(control),
            "points": distribution([x[0] for x in sample]),
            "positive": probability(sum(x[0] > 0 for x in sample), n),
            "negative": probability(sum(x[0] < 0 for x in sample), n),
            "mfe_long_points": distribution([x[1] for x in sample]),
            "mae_long_points": distribution([x[2] for x in sample]),
            "targets": {
                target: {
                    "up": probability(sum(x[1] >= target for x in sample), n),
                    "down": probability(sum(x[2] >= target for x in sample), n),
                }
                for target in TARGETS
            },
            "baseline_positive": probability(sum(x[0] > 0 for x in control), len(control)),
            "exploratory_positive_p_bonferroni": min(1, p * len(HORIZONS))
            if p is not None
            else None,
        }
    current = {}
    for name, value in rows[-1].items():
        history = [r[name] for r in rows[:-1] if r[name] is not None]
        current[name] = {
            "value": value,
            "percentile": percentile(value, history),
            "z_score": zscore(value, history),
            "deviation_from_mean": value - mean(history) if history and value is not None else None,
        }
    return {
        "conditions": conditions,
        "candles": len(bars),
        "candidate_crossings": len(crossings),
        "current": current,
        "horizons": horizons,
        "eligible_feature_rows": sum(eligible),
        "observed_cvd_changes": sum(r["volume_delta"] is not None for r in rows),
        "method": "Crossing the filter after 100-bar warmup; no overlapping outcome windows "
        "or gaps. Exploratory tests adjust across horizons within this query. "
        "Serial dependence can persist. Points exclude costs and execution.",
    }
