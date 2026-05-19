"""
Тесты exchanges/bitget_ws.py
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from exchanges.bitget_ws import BitgetWsOrderBookConnection


@pytest.fixture
def bitget_settings():
    return {
        "exchange_name": "bitget",
        "ws_url": "wss://ws.bitget.com/v2/ws/public",
        "rest_orderbook_url": "https://api.bitget.com/api/v2/spot/market/orderbook",
        "book_type": "step0",
        "orderbook_depth": 200,
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestSendSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_message(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT", "BTCUSDT"])
        ws = AsyncMock()
        await conn.send_subscribe(ws)
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.call_args[0][0])
        assert sent["op"] == "subscribe"
        args = sent["args"]
        channels = {a["channel"] for a in args}
        # books200 невалиден для Bitget v2; должен fallback на "books"
        assert "books" in channels
        assert "books200" not in channels


class TestHandleSystemMessage:
    @pytest.mark.asyncio
    async def test_subscribe_event_ignored(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"event": "subscribe"})
        assert result is True

    @pytest.mark.asyncio
    async def test_pong_ignored(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, "pong")
        assert result is True

    @pytest.mark.asyncio
    async def test_data_message_not_system(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        ws = AsyncMock()
        result = await conn.handle_system_message(ws, {"arg": {"channel": "books"}, "data": []})
        assert result is False


class TestExtractUpdates:
    def test_snapshot_update(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        message = {
            "action": "snapshot",
            "arg": {"instId": "SOLUSDT"},
            "data": [{
                "asks": [["142.50", "5.0"]],
                "bids": [["142.40", "3.0"]],
                "seq": 12345,
                "ts": 1000,
            }],
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "SOLUSDT"
        assert updates[0]["type"] == "snapshot"
        assert updates[0]["sequence"] == 12345

    def test_delta_update(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        message = {
            "action": "update",
            "arg": {"instId": "SOLUSDT"},
            "data": [{
                "asks": [["142.55", "2.0"]],
                "bids": [],
                "seq": 12346,
            }],
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["type"] == "delta"
        assert updates[0]["sequence"] == 12346

    def test_empty_data_skipped(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        message = {"arg": {"instId": "SOLUSDT"}, "data": []}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_dict_payload(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        with patch("exchanges.bitget_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "data": {
                    "asks": [["142.50", "5.0"]],
                    "bids": [["142.40", "3.0"]],
                    "seq": 999,
                }
            }
            result = await conn.fetch_rest_snapshot("SOLUSDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1

    @pytest.mark.asyncio
    async def test_fetch_parses_list_payload(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        with patch("exchanges.bitget_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "data": [{
                    "asks": [["142.50", "5.0"]],
                    "bids": [["142.40", "3.0"]],
                    "seq": 999,
                }]
            }
            result = await conn.fetch_rest_snapshot("SOLUSDT")
            asks, bids, seq = result
            assert seq == 999

    @pytest.mark.asyncio
    async def test_invalid_payload_returns_none(self, bitget_settings, mock_store):
        conn = BitgetWsOrderBookConnection(bitget_settings, mock_store, 1, ["SOLUSDT"])
        with patch("exchanges.bitget_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = "invalid"
            result = await conn.fetch_rest_snapshot("SOLUSDT")
            assert result is None
