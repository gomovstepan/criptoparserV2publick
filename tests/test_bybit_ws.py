"""
Тесты exchanges/bybit_ws.py
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchanges.bybit_ws import BybitWsOrderBookConnection


@pytest.fixture
def bybit_settings():
    return {
        "exchange_name": "bybit",
        "ws_url": "wss://stream.bybit.com/v5/public",
        "rest_orderbook_url": "https://api.bybit.com/v5/market/orderbook",
        "category": "spot",
        "orderbook_depth": 50,
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestBuildWsUrl:
    def test_appends_spot_when_missing(self, bybit_settings, mock_store):
        bybit_settings["ws_url"] = "wss://stream.bybit.com/v5/public"
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        assert conn.build_ws_url() == "wss://stream.bybit.com/v5/public/spot"

    def test_preserves_existing_spot_suffix(self, bybit_settings, mock_store):
        bybit_settings["ws_url"] = "wss://stream.bybit.com/v5/public/spot"
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        assert conn.build_ws_url() == "wss://stream.bybit.com/v5/public/spot"

    def test_handles_trailing_slash(self, bybit_settings, mock_store):
        bybit_settings["ws_url"] = "wss://stream.bybit.com/v5/public/"
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        assert conn.build_ws_url() == "wss://stream.bybit.com/v5/public/spot"


class TestSendSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_message(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT", "BTCUSDT"])
        ws = AsyncMock()
        await conn.send_subscribe(ws)
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "subscribe"
        assert "orderbook.50.SOLUSDT" in sent["args"]
        assert "orderbook.50.BTCUSDT" in sent["args"]


class TestHandleSystemMessage:
    @pytest.mark.asyncio
    async def test_ping_pong(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"op": "ping"})
        assert result is True
        ws.send.assert_awaited_once_with('{"op":"pong"}')

    @pytest.mark.asyncio
    async def test_subscription_response_ignored(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"op": "subscribe", "success": True})
        assert result is True

    @pytest.mark.asyncio
    async def test_data_message_not_system(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"topic": "orderbook.50.SOLUSDT"})
        assert result is False


class TestExtractUpdates:
    def test_snapshot_update(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        message = {
            "topic": "orderbook.50.SOLUSDT",
            "type": "snapshot",
            "data": {
                "a": [["142.50", "5.0"]],
                "b": [["142.40", "3.0"]],
                "u": 12345,
            },
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "SOLUSDT"
        assert updates[0]["type"] == "snapshot"
        assert updates[0]["sequence"] == 12345

    def test_delta_update(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        message = {
            "topic": "orderbook.50.SOLUSDT",
            "type": "delta",
            "data": {
                "a": [["142.55", "2.0"]],
                "b": [],
                "u": 12346,
            },
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["type"] == "delta"
        assert updates[0]["sequence"] == 12346

    def test_non_orderbook_topic_skipped(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        message = {"topic": "tickers.SOLUSDT"}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_result(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        with patch("exchanges.bybit_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "result": {
                    "a": [["142.50", "5.0"]],
                    "b": [["142.40", "3.0"]],
                    "u": 999,
                }
            }
            result = await conn.fetch_rest_snapshot("SOLUSDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1
            mock_get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_none(self, bybit_settings, mock_store):
        conn = BybitWsOrderBookConnection(bybit_settings, mock_store, 1, ["SOLUSDT"])
        with patch("exchanges.bybit_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "invalid"
            result = await conn.fetch_rest_snapshot("SOLUSDT")
            assert result is None
