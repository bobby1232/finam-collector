import json
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


def sessions():
    path = os.getenv("MARKET_V2_SESSIONS_FILE")
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text())
        return {
            market: {
                date: [(parse_time(start), parse_time(end)) for start, end in intervals]
                for date, intervals in days.items()
            }
            for market, days in raw.items()
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Session schedule must specify offsets")
    return result.astimezone(UTC)


def session_context(symbol, as_of, schedule):
    market = symbol.split("@", 1)[1]
    local_date = as_of.astimezone(MOSCOW).date().isoformat()
    days = schedule.get(market, {})
    today = sorted(days.get(local_date, []))
    active = next(((start, end) for start, end in today if start <= as_of < end), None)
    previous = [
        sorted(intervals)
        for date, intervals in sorted(days.items())
        if date < local_date and intervals
    ]
    status = "OPEN" if active else "MARKET_CLOSED" if local_date in days else "UNKNOWN"
    return {
        "status": status,
        "timestamp": as_of,
        "intervals": today,
        "session_start": today[0][0] if today else None,
        "previous_day_intervals": previous[-1] if previous else [],
        "source": "configured_exchange_schedule" if local_date in days else None,
    }
