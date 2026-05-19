"""
Тесты exchanges/kucoin_ws.py
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchanges.kucoin_ws import KucoinWsOrderBookConnection


@pytest.fixture
def kucoin_settings():
    return {
        "exchange_name": "kucoin",
        "ws_url": "wss://ws-api-spot.kucoin.com",
        "bullet_public_url": "https://api.kucoin.com/api/v1/bullet-public",
        "rest_orderbook_url": "https://api.kucoin.com/api/v1/market/orderbook/level2_100",
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestResolveWsUrl:
    @pytest.mark.asyncio
    async def test_fetches_bullet_token_and_uses_it(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.kucoin_ws.http_post_json", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {
                "data": {
                    "token": "test-token-123",
                    "instanceServers": [{"endpoint": "wss://ws-api-spot.kucoin.com"}],
                }
            }
            url = await conn._resolve_ws_url()
            assert "wss://ws-api-spot.kucoin.com" in url
            assert "test-token-123" in url
            assert "connectId=cpv2-1" in url

    @pytest.mark.asyncio
    async def test_fallback_to_configured_url_when_bullet_fails(self, kucoin_settings, mock_store):
        kucoin_settings["ws_url"] = "wss://custom-kucoin.example.com"
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.kucoin_ws.http_post_json", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = RuntimeError("network error")
            url = await conn._resolve_ws_url()
            assert url == "wss://custom-kucoin.example.com"

    @pytest.mark.asyncio
    async def test_fallback_to_default_url_when_bullet_returns_no_token(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.kucoin_ws.http_post_json", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = {"data": {}}
            url = await conn._resolve_ws_url()
            assert "ws-api-spot.kucoin.com" in url


class TestBuildWsUrl:
    def test_returns_resolved_url(self, kucoin_settings, mock_store):
        kucoin_settings["ws_url"] = "wss://ws-api-spot.kucoin.com/endpoint"
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        conn._resolved_ws_url = "wss://resolved"
        assert conn.build_ws_url() == "wss://resolved"

    def test_falls_back_to_settings(self, kucoin_settings, mock_store):
        kucoin_settings["ws_url"] = "wss://fallback"
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        assert conn.build_ws_url() == "wss://fallback"


class TestSendSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_message(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT", "ETH-USDT"])
        ws = AsyncMock()
        await conn.send_subscribe(ws)
        assert ws.send.await_count == 2
        calls = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        topics = {c["topic"] for c in calls}
        assert "/market/level2:BTC-USDT" in topics
        assert "/market/level2:ETH-USDT" in topics


class TestHandleSystemMessage:
    @pytest.mark.asyncio
    async def test_ping_pong(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"type": "ping"})
        assert result is True
        ws.send.assert_awaited_once()
        assert json.loads(ws.send.call_args[0][0])["type"] == "pong"

    @pytest.mark.asyncio
    async def test_welcome_ignored(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"type": "welcome"})
        assert result is True

    @pytest.mark.asyncio
    async def test_data_message_not_system(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"type": "message", "topic": "/market/level2:BTC-USDT"})
        assert result is False


class TestExtractUpdates:
    def test_delta_with_changes(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        message = {
            "type": "message",
            "topic": "/market/level2:BTC-USDT",
            "data": {
                "changes": {
                    "asks": [["101.0", "1.5"]],
                    "bids": [["99.0", "2.0"]],
                },
                "sequenceEnd": 12345,
                "time": 1000,
            },
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "BTC-USDT"
        assert updates[0]["sequence"] == 12345
        assert updates[0]["type"] == "delta"

    def test_non_orderbook_topic_skipped(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        message = {"topic": "/market/ticker:BTC-USDT"}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_payload(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.kucoin_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "data": {
                    "asks": [["101.0", "1.5"]],
                    "bids": [["99.0", "2.0"]],
                    "sequence": 999,
                }
            }
            result = await conn.fetch_rest_snapshot("BTC-USDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1
            mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_none(self, kucoin_settings, mock_store):
        conn = KucoinWsOrderBookConnection(kucoin_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.kucoin_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "invalid"
            result = await conn.fetch_rest_snapshot("BTC-USDT")
            assert result is None
