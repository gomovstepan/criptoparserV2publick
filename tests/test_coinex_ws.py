"""
Тесты exchanges/coinex_ws.py
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from exchanges.coinex_ws import CoinexWsOrderBookConnection


@pytest.fixture
def coinex_settings():
    return {
        "exchange_name": "coinex",
        "ws_url": "wss://socket.coinex.com/",
        "rest_orderbook_url": "https://api.coinex.com/v1/market/depth",
        "depth_limit": 20,
        "price_interval": "0",
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestSendSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_message(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT", "ETHUSDT"])
        ws = AsyncMock()
        await conn.send_subscribe(ws)
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["method"] == "depth.subscribe_multi"
        assert len(sent["params"]) == 2
        assert sent["params"][0][0] == "BTCUSDT"
        assert sent["params"][1][0] == "ETHUSDT"


class TestHandleSystemMessage:
    @pytest.mark.asyncio
    async def test_subscribe_ack_ignored(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"method": "depth.subscribe_multi", "id": 1})
        assert result is True

    @pytest.mark.asyncio
    async def test_server_ping_pong(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"method": "server.ping"})
        assert result is True
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["method"] == "server.pong"

    @pytest.mark.asyncio
    async def test_error_ignored(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"error": "some_error"})
        assert result is True

    @pytest.mark.asyncio
    async def test_data_message_not_system(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"method": "depth.update", "params": [False, {}, "BTCUSDT"]})
        assert result is False


class TestExtractUpdates:
    def test_snapshot_update(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        message = {
            "method": "depth.update",
            "params": [
                True,
                {"asks": [["101.0", "1.5"]], "bids": [["99.0", "2.0"]], "time": 12345},
                "BTCUSDT",
            ],
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "BTCUSDT"
        assert updates[0]["type"] == "snapshot"
        assert updates[0]["sequence"] == 12345

    def test_delta_update(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        message = {
            "method": "depth.update",
            "params": [
                False,
                {"asks": [["101.5", "1.0"]], "bids": [], "time": 12346},
                "BTCUSDT",
            ],
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["type"] == "delta"
        assert updates[0]["sequence"] == 12346

    def test_non_depth_method_skipped(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        message = {"method": "server.ping"}
        updates = conn.extract_updates(message)
        assert updates == []

    def test_short_params_skipped(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        message = {"method": "depth.update", "params": [False]}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_payload(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        with patch("exchanges.coinex_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "data": {
                    "asks": [["101.0", "1.5"]],
                    "bids": [["99.0", "2.0"]],
                    "time": 999,
                }
            }
            result = await conn.fetch_rest_snapshot("BTCUSDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_none(self, coinex_settings, mock_store):
        conn = CoinexWsOrderBookConnection(coinex_settings, mock_store, 1, ["BTCUSDT"])
        with patch("exchanges.coinex_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "invalid"
            result = await conn.fetch_rest_snapshot("BTCUSDT")
            assert result is None
