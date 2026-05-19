"""
Тесты exchanges/websocket_base.py
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchanges.websocket_base import WebSocketOrderBookConnection


class FakeWsConnection(WebSocketOrderBookConnection):
    """Фейковая реализация для тестов базового класса."""

    def build_ws_url(self):
        return "wss://test.example/ws"

    async def send_subscribe(self, ws):
        pass

    async def fetch_rest_snapshot(self, symbol):
        return (
            [["100", "1"], ["101", "2"]],
            [["99", "1"], ["98", "2"]],
            1,
        )

    def extract_updates(self, message):
        data = message.get("data", {})
        return [{
            "symbol": data.get("s", "BTCUSDT"),
            "type": data.get("type", "delta"),
            "asks": data.get("a", []),
            "bids": data.get("b", []),
            "sequence": data.get("u"),
        }]


@pytest.fixture
def fake_settings():
    return {
        "exchange_name": "testex",
        "reconnect_delay_seconds": 0.01,
        "ws_ping_interval_seconds": 5,
        "ws_ping_timeout_seconds": 5,
    }


@pytest.fixture
def fake_store():
    return AsyncMock()


class TestBootstrapFromRest:
    @pytest.mark.asyncio
    async def test_writes_snapshot_for_each_symbol(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT", "ETHUSDT"])
        await conn.bootstrap_from_rest()
        assert fake_store.write_orderbook.call_count == 2
        calls = fake_store.write_orderbook.call_args_list
        assert calls[0].kwargs["message_type"] == "snapshot"
        assert calls[0].kwargs["symbol"] == "BTCUSDT"
        assert calls[1].kwargs["symbol"] == "ETHUSDT"

    @pytest.mark.asyncio
    async def test_state_marked_ready(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        await conn.bootstrap_from_rest()
        assert conn.state["BTCUSDT"]["ready"] is True
        assert conn.state["BTCUSDT"]["last_sequence"] == 1


class TestApplyUpdate:
    @pytest.mark.asyncio
    async def test_delta_applied(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        conn.state["BTCUSDT"]["ready"] = True
        conn.state["BTCUSDT"]["last_sequence"] = 5

        await conn.apply_update({
            "symbol": "BTCUSDT",
            "type": "delta",
            "asks": [["101", "1"]],
            "bids": [["99", "1"]],
            "sequence": 6,
        })

        fake_store.write_orderbook.assert_awaited_once()
        assert fake_store.write_orderbook.call_args.kwargs["message_type"] == "delta"
        assert conn.state["BTCUSDT"]["last_sequence"] == 6

    @pytest.mark.asyncio
    async def test_snapshot_always_valid(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        conn.state["BTCUSDT"]["last_sequence"] = 100

        await conn.apply_update({
            "symbol": "BTCUSDT",
            "type": "snapshot",
            "asks": [["100", "1"]],
            "bids": [["99", "1"]],
            "sequence": 1,
        })

        assert fake_store.write_orderbook.call_args.kwargs["message_type"] == "snapshot"

    @pytest.mark.asyncio
    async def test_sequence_gap_triggers_recovery(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        conn.state["BTCUSDT"]["ready"] = True
        conn.state["BTCUSDT"]["last_sequence"] = 10

        await conn.apply_update({
            "symbol": "BTCUSDT",
            "type": "delta",
            "asks": [["101", "1"]],
            "bids": [["99", "1"]],
            "sequence": 5,  # gap!
        })

        # recovery вызывает write_orderbook("snapshot")
        assert fake_store.write_orderbook.call_count == 1
        assert fake_store.write_orderbook.call_args.kwargs["message_type"] == "snapshot"


class TestRunForever:
    @pytest.mark.asyncio
    async def test_reconnect_on_error(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        conn.reconnect_delay_seconds = 0.001

        # Первая итерация падает, вторая тоже, но ловим после 2 reconnect
        call_count = 0
        original_bootstrap = conn.bootstrap_from_rest

        async def failing_bootstrap():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("boom")
            await original_bootstrap()
            raise asyncio.CancelledError()  # выходим из цикла

        conn.bootstrap_from_rest = failing_bootstrap

        with pytest.raises(asyncio.CancelledError):
            await conn.run_forever()

        assert call_count == 3
        assert conn.reconnects == 2

    @pytest.mark.asyncio
    async def test_reconnect_on_connection_closed(self, fake_settings, fake_store):
        import websockets

        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        conn.reconnect_delay_seconds = 0.001

        call_count = 0
        original_bootstrap = conn.bootstrap_from_rest

        async def failing_bootstrap():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise websockets.ConnectionClosed(
                    websockets.frames.Close(1000, "Bye"),
                    websockets.frames.Close(1000, "Bye"),
                )
            await original_bootstrap()
            raise asyncio.CancelledError()

        conn.bootstrap_from_rest = failing_bootstrap

        with pytest.raises(asyncio.CancelledError):
            await conn.run_forever()

        assert call_count == 2
        assert conn.reconnects == 1


class TestDecodeMessage:
    def test_json_string(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.decode_message('{"a": 1}') == {"a": 1}

    def test_json_bytes(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.decode_message(b'{"a": 1}') == {"a": 1}

    def test_invalid_returns_none(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.decode_message("not json") is None


class TestSequenceValidation:
    def test_none_sequence_is_valid(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.is_sequence_valid("BTCUSDT", "delta", None, 5) is True

    def test_snapshot_is_valid(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.is_sequence_valid("BTCUSDT", "snapshot", 1, 100) is True

    def test_equal_sequence_is_invalid(self, fake_settings, fake_store):
        """Равный sequence считается невалидным — предотвращает double-apply дублированных WS сообщений."""
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.is_sequence_valid("BTCUSDT", "delta", 5, 5) is False

    def test_greater_sequence_is_valid(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.is_sequence_valid("BTCUSDT", "delta", 10, 5) is True

    def test_lower_sequence_is_invalid(self, fake_settings, fake_store):
        conn = FakeWsConnection(fake_settings, fake_store, 1, ["BTCUSDT"])
        assert conn.is_sequence_valid("BTCUSDT", "delta", 3, 5) is False
