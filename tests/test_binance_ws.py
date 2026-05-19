"""
Тесты exchanges/binance_ws.py
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchanges.binance_ws import BinanceWsOrderBookConnection


@pytest.fixture
def binance_settings():
    return {
        "exchange_name": "binance",
        "ws_url": "wss://stream.binance.com:9443/ws",
        "rest_orderbook_url": "https://api.binance.com/api/v3/depth",
        "orderbook_depth": 20,
        "reconnect_delay_seconds": 2,
    }


@pytest.fixture
def mock_store():
    return AsyncMock()


class TestBuildWsUrl:
    def test_single_symbol(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT"])
        assert conn.build_ws_url() == "wss://stream.binance.com:9443/ws/btcusdt@depth@100ms"

    def test_multiple_symbols(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT", "ETHUSDT"])
        assert conn.build_ws_url() == "wss://stream.binance.com:9443/ws/btcusdt@depth@100ms/ethusdt@depth@100ms"


class TestExtractUpdates:
    def test_combined_stream_wrapper(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT"])
        message = {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "s": "BTCUSDT",
                "u": 12345,
                "a": [["101.0", "1.5"]],
                "b": [["99.0", "2.0"]],
            },
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "BTCUSDT"
        assert updates[0]["sequence"] == 12345
        assert updates[0]["type"] == "delta"

    def test_raw_message(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT"])
        message = {
            "s": "BTCUSDT",
            "u": 12345,
            "a": [["101.0", "1.5"]],
            "b": [["99.0", "2.0"]],
        }
        updates = conn.extract_updates(message)
        assert len(updates) == 1
        assert updates[0]["symbol"] == "BTCUSDT"

    def test_empty_symbol_skipped(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT"])
        message = {"a": [], "b": []}
        updates = conn.extract_updates(message)
        assert updates == []


class TestFetchRestSnapshot:
    @pytest.mark.asyncio
    async def test_fetch_parses_payload(self, binance_settings, mock_store):
        conn = BinanceWsOrderBookConnection(binance_settings, mock_store, 1, ["BTCUSDT"])
        with patch("exchanges.binance_ws.http_get_json", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "lastUpdateId": 999,
                "asks": [["101.0", "1.5"]],
                "bids": [["99.0", "2.0"]],
            }
            result = await conn.fetch_rest_snapshot("BTCUSDT")
            asks, bids, seq = result
            assert seq == 999
            assert len(asks) == 1
            assert len(bids) == 1
            mock_get.assert_awaited_once()
