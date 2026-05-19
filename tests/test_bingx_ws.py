"""
Тесты exchanges/bingx_ws.py
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from exchanges.bingx_ws import BingxWsOrderBookConnection


@pytest.fixture
def bingx_settings():
    return {
        "exchange_name": "bingx",
        "ws_url": "wss://open-api-ws.bingx.com/market",
        "rest_orderbook_url": "https://open-api.bingx.com/openApi/spot/v1/market/depth",
        "orderbook_depth": 20,
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestSendSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_message(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT", "ETH-USDT"])
        ws = AsyncMock()
        await conn.send_subscribe(ws)
        assert ws.send.await_count == 2
        calls = [json.loads(c[0][0]) for c in ws.send.call_args_list]
        data_types = {c["dataType"] for c in calls}
        assert "BTC-USDT@depth100" in data_types
        assert "ETH-USDT@depth100" in data_types


class TestHandleSystemMessage:
    @pytest.mark.asyncio
    async def test_ping_pong(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"ping": 123456})
        assert result is True
        ws.send.assert_awaited_once()
        assert json.loads(ws.send.call_args[0][0])["pong"] == 123456

    @pytest.mark.asyncio
    async def test_data_message_not_system(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"dataType": "BTC-USDT@depth100", "data": {}})
        assert result is False


class TestExtractUpdates:
    def test_depth_update(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        message = {
            "dataType": "BTC-USDT@depth100",
            "data": {
                "asks": [["101.0", "1.5"]],
                "bids": [["99.0", "2.0"]],
                "u": 12345,
                "E": 1000,
            },
            "ts": 1001,
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "BTC-USDT"
        assert updates[0]["sequence"] == 12345
        assert updates[0]["type"] == "delta"
        assert updates[0]["exchange_ts"] == 1000

    def test_non_depth_skipped(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        message = {"dataType": "BTC-USDT@trade", "data": {}}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_payload(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.bingx_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "data": {
                    "asks": [["101.0", "1.5"]],
                    "bids": [["99.0", "2.0"]],
                    "u": 999,
                }
            }
            result = await conn.fetch_rest_snapshot("BTC-USDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_none(self, bingx_settings, mock_store):
        conn = BingxWsOrderBookConnection(bingx_settings, mock_store, 1, ["BTC-USDT"])
        with patch("exchanges.bingx_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "invalid"
            result = await conn.fetch_rest_snapshot("BTC-USDT")
            assert result is None
