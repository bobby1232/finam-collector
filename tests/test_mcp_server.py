from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from finam_mcp.config import Settings
from finam_mcp.server import create_server


class FakeCollector:
    async def close(self):
        return None

    async def health(self):
        return {"status": "ok", "poll_seconds": 30, "last_cycle": "2026-11-18T12:00:00Z"}

    async def bars(self, symbol, interval, limit):
        return [
            {
                "timestamp": "2026-11-18T12:00:00+00:00",
                "open": 83000.0,
                "high": 83100.0,
                "low": 82900.0,
                "close": 83050.0,
                "volume": 10.0,
                "source": "finam",
                "updated_at": "2026-11-18T12:00:10+00:00",
            }
        ]


class StaticVerifier:
    async def verify_token(self, token):
        return AccessToken(
            token=token,
            client_id="chatgpt-finam-mcp",
            subject="user-1",
            scopes=["finam:read"],
            resource="https://testserver/mcp",
        )


def settings() -> Settings:
    return Settings(
        collector_base_url="http://collector.test",
        mcp_resource_url="https://testserver/mcp",
        oauth_issuer_url="https://testserver/oauth/realms/finam",
    )


def test_oauth_resource_metadata_and_unauthorized_challenge():
    app = create_server(settings(), FakeCollector()).streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True
    )
    with TestClient(app) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json() == {
            "resource": "https://testserver/mcp",
            "authorization_servers": ["https://testserver/oauth/realms/finam"],
            "scopes_supported": ["finam:read"],
            "bearer_methods_supported": ["header"],
        }

        denied = client.get("/mcp")
        assert denied.status_code == 401
        assert (
            'resource_metadata="https://testserver/.well-known/oauth-protected-resource/mcp"'
            in denied.headers["www-authenticate"]
        )


def test_health_does_not_disclose_secrets():
    app = create_server(settings(), FakeCollector()).streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True
    )
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        payload = response.json()
        assert payload["oauth_required"] is True
        assert "token" not in response.text.lower()


def test_initialize_tools_list_and_stale_quote_call():
    app = create_server(settings(), FakeCollector(), StaticVerifier()).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="testserver",
    )
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "finam-mcp"

        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools = listed.json()["result"]["tools"]
        assert {item["name"] for item in tools} == {
            "get_si_quote",
            "get_si_rsi",
            "get_si_candles",
            "get_si_analysis",
            "get_si_oversold_research",
            "get_collector_health",
        }
        assert all(item["annotations"]["readOnlyHint"] for item in tools)

        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_si_quote",
                    "arguments": {"max_age_seconds": 30},
                },
            },
        )
        quote = called.json()["result"]["structuredContent"]
        assert quote["is_fresh"] is False
        assert quote["data_quality_status"] == "stale"
        assert quote["price_semantics"].startswith("close-so-far")


def test_rate_limit_rejects_second_tool_call():
    limited = settings().model_copy(update={"rate_limit_requests": 1})
    app = create_server(limited, FakeCollector(), StaticVerifier()).streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="testserver",
    )
    headers = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_si_quote", "arguments": {}},
    }
    with TestClient(app) as client:
        assert client.post("/mcp", headers=headers, json=call).json()["result"]["isError"] is False
        call["id"] = 2
        result = client.post("/mcp", headers=headers, json=call).json()["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == "Error executing tool get_si_quote"
