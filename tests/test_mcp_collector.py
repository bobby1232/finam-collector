import json

import httpx
import pytest

from finam_mcp.collector import CollectorClient
from finam_mcp.config import Settings


def settings() -> Settings:
    return Settings(
        collector_base_url="http://collector.test",
        mcp_resource_url="https://testserver/mcp",
        oauth_issuer_url="https://testserver/oauth/realms/finam",
        research_api_token="x" * 32,
    )


@pytest.mark.asyncio
async def test_one_minute_bars_are_normalized_and_sorted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bars"
        payload = {
            "bars": [
                {
                    "timestamp": "2026-11-18T12:01:00Z",
                    "open": "2",
                    "high": "3",
                    "low": "1",
                    "close": "2.5",
                    "volume": "10",
                    "source": "finam",
                    "updated_at": "2026-11-18T12:01:10Z",
                },
                {
                    "timestamp": "2026-11-18T12:00:00Z",
                    "open": "1",
                    "high": "2",
                    "low": "0.5",
                    "close": "1.5",
                    "volume": None,
                    "source": "finam",
                    "updated_at": "2026-11-18T12:00:10Z",
                },
            ]
        }
        return httpx.Response(200, content=json.dumps(payload))

    client = CollectorClient(settings(), transport=httpx.MockTransport(handler))
    try:
        bars = await client.bars("SiZ6@RTSX", "1m", 2)
    finally:
        await client.close()
    assert [x["close"] for x in bars] == [1.5, 2.5]
    assert bars[0]["volume"] is None


@pytest.mark.asyncio
async def test_unknown_symbol_is_rejected_without_network_call():
    client = CollectorClient(settings(), transport=httpx.MockTransport(lambda _: None))
    try:
        with pytest.raises(ValueError, match="Unsupported symbol"):
            await client.bars("GAZP@MISX", "1m", 1)
    finally:
        await client.close()
