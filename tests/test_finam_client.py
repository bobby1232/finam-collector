from datetime import datetime

import httpx
import pytest

from app.finam import FinamClient


@pytest.mark.asyncio
async def test_retries_429_and_honors_retry_after():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"token": "test-token"})
        if len([r for r in requests if r.url.path.endswith("/bars")]) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"bars": []})

    client = FinamClient(
        "secret",
        transport=httpx.MockTransport(handler),
        backoff_base_seconds=0,
        max_requests_per_second=1_000_000,
    )
    try:
        bars = await client.bars(
            "SBER@MISX",
            "TIME_FRAME_M1",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
    finally:
        await client.close()

    assert bars == []
    assert client.stats["http_429_count"] == 1
    assert client.stats["retry_count"] == 1
    assert len([r for r in requests if r.url.path.endswith("/bars")]) == 2


@pytest.mark.asyncio
async def test_refreshes_token_once_after_401():
    auth_calls = 0
    bars_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, bars_calls
        if request.url.path == "/v1/sessions":
            auth_calls += 1
            return httpx.Response(200, json={"token": f"token-{auth_calls}"})
        bars_calls += 1
        if bars_calls == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"bars": []})

    client = FinamClient(
        "secret",
        transport=httpx.MockTransport(handler),
        max_requests_per_second=1_000_000,
    )
    try:
        await client.bars(
            "SBER@MISX",
            "TIME_FRAME_M1",
            datetime(2026, 1, 1),
            datetime(2026, 1, 2),
        )
    finally:
        await client.close()

    assert auth_calls == 2
    assert bars_calls == 2
