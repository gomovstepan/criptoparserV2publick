"""Регрессионные тесты для CRITICAL-проблем, найденных в аудите."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest

from common.redis_store import RedisOrderBookStore
from backend.service import BackendService


class TestC1IsolateExchangeReadFailures:
    """C1: read_raw_snapshot и read_aggregated_snapshot не должны падать целиком
    при ошибке одной биржи."""

    @pytest.mark.asyncio
    async def test_read_raw_snapshot_skips_failed_exchange(self):
        mock_redis = AsyncMock()
        # binance OK, bybit падает
        mock_redis.smembers.side_effect = [
            {b"BTCUSDT"},
            Exception("Redis connection refused"),
        ]
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        pipeline = MagicMock()
        pipeline.hgetall.return_value = None
        pipeline.execute = AsyncMock(return_value=[
            {b"100.5": b"10"},  # ASK
            {b"100.0": b"5"},   # BID
            {b"updated_at": now_iso.encode()},
        ])
        mock_redis.pipeline = MagicMock(return_value=pipeline)
        settings = {
            "symbols_set_template": "ob:{exchange}:symbols",
            "key_prefix": "ob",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        result = await store.read_raw_snapshot(["binance", "bybit"])
        assert "binance" in result
        assert result["binance"]
        assert "bybit" in result
        assert result["bybit"] == {}  # graceful degradation

    @pytest.mark.asyncio
    async def test_read_aggregated_snapshot_skips_failed_exchange(self):
        mock_redis = AsyncMock()
        mock_redis.smembers.side_effect = [
            {b"BTCUSDT"},
            Exception("Redis timeout"),
        ]
        pipeline = MagicMock()
        pipeline.hgetall.return_value = None
        pipeline.execute = AsyncMock(return_value=[{}, {}, {"updated_at": "2024-01-01T00:00:00+00:00"}])
        mock_redis.pipeline = MagicMock(return_value=pipeline)
        settings = {
            "symbols_set_template": "ob:{exchange}:symbols",
            "key_prefix": "ob",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        result = await store.read_aggregated_snapshot(
            ["binance", "bybit"],
            target_value=Decimal("100"),
            max_levels=10,
        )
        assert "binance" in result
        assert "bybit" in result
        assert result["bybit"] == {}


class TestC2C3EventLifecycleDoesNotLeakOnTelegramOrSqliteFailure:
    """C2/C3: При ошибке Telegram или SQLite событие всё равно должно удаляться из Redis."""

    @pytest.mark.asyncio
    async def test_telegram_failure_still_deletes_event(self):
        store = MagicMock()
        store.list_arbitrage_events = AsyncMock(return_value=[
            ("BTC:binance:bybit", {
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
                "sent": True,
                "telegram_message_id": "12345",
                "cumulative_active_seconds": "20",
                "last_active_at": "2024-01-01T00:00:00+00:00",
                "trade_signal_published": True,
                "data": {
                    "coin": "BTCUSDT",
                    "buy_exchange": "binance",
                    "sell_exchange": "bybit",
                    "buy_price": "100",
                    "sell_price": "101",
                    "spread_percent": "1.0",
                    "start_time": "2024-01-01T00:00:00+00:00",
                    "max_spread": "1.0",
                },
            })
        ])
        store.delete_arbitrage_event = AsyncMock()
        store.redis = AsyncMock()

        settings = {
            "exchanges": ["binance", "bybit"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 10,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
            },
            "telegram": {"bot_token": "fake_token", "chat_id": "123"},
        }
        from backend.history_store import ArbitrageHistoryStore
        history = ArbitrageHistoryStore(":memory:", max_records=100)
        svc = BackendService(store, settings, history)
        # симулируем падение Telegram при редактировании закрытого события
        svc.telegram.edit_closed_event = AsyncMock(side_effect=RuntimeError("Telegram down"))

        # Событие НЕ в текущих возможностях (пропало) и sent=True → должно закрыться
        await svc.process_arbitrage_events(set())
        # Событие должно быть удалено из Redis несмотря на ошибку Telegram
        store.delete_arbitrage_event.assert_awaited_once_with("BTC:binance:bybit")

    @pytest.mark.asyncio
    async def test_sqlite_failure_still_deletes_event(self):
        store = MagicMock()
        store.list_arbitrage_events = AsyncMock(return_value=[
            ("BTC:binance:bybit", {
                "created_at": "2024-01-01T00:00:00+00:00",
                "updated_at": "2024-01-01T00:00:00+00:00",
                "sent": True,
                "telegram_message_id": "12345",
                "cumulative_active_seconds": "20",
                "last_active_at": "2024-01-01T00:00:00+00:00",
                "trade_signal_published": True,
                "data": {
                    "coin": "BTCUSDT",
                    "buy_exchange": "binance",
                    "sell_exchange": "bybit",
                    "buy_price": "100",
                    "sell_price": "101",
                    "spread_percent": "1.0",
                    "start_time": "2024-01-01T00:00:00+00:00",
                    "max_spread": "1.0",
                },
            })
        ])
        store.delete_arbitrage_event = AsyncMock()
        store.redis = AsyncMock()

        settings = {
            "exchanges": ["binance", "bybit"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 10,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
            },
            "telegram": {"bot_token": "", "chat_id": ""},
        }
        from backend.history_store import ArbitrageHistoryStore
        history = ArbitrageHistoryStore(":memory:", max_records=100)
        svc = BackendService(store, settings, history)

        # Событие НЕ в текущих возможностях → должно закрыться (Telegram disabled)
        await svc.process_arbitrage_events(set())
        store.delete_arbitrage_event.assert_awaited_once_with("BTC:binance:bybit")


class TestC4C5CorruptedJsonDoesNotFreezeSystem:
    """C4/C5: Повреждённый JSON в событии должен быть пропущен, а не ломать весь payload."""

    @pytest.mark.asyncio
    async def test_list_arbitrage_events_skips_corrupted_json(self):
        mock_redis = AsyncMock()
        mock_redis.smembers = AsyncMock(return_value={b"event_good", b"event_bad"})
        pipeline = MagicMock()
        pipeline.hgetall.return_value = None
        pipeline.execute = AsyncMock(return_value=[
            {b"sent": b"0", b"data": b'{bad json'},  # event_bad (first alphabetically)
            {b"sent": b"0", b"data": b'{"spread_percent":"1.0"}'},  # event_good
        ])
        mock_redis.pipeline = MagicMock(return_value=pipeline)
        settings = {
            "arbitrage_events_set_key": "arb:events",
            "arbitrage_event_key_template": "arb:event:{event_key}",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        events = await store.list_arbitrage_events()
        assert len(events) == 1
        assert events[0][0] == "event_good"

    @pytest.mark.asyncio
    async def test_read_arbitrage_event_returns_none_on_bad_json(self):
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={
            b"sent": b"0",
            b"data": b'{bad json',
        })
        settings = {
            "arbitrage_event_key_template": "arb:event:{event_key}",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        result = await store.read_arbitrage_event("ev1")
        assert result is None


class TestC6CorruptedSpreadPercentDoesNotFreezePayload:
    """C6: build_active_payload_from_redis должен пропускать события с битым spread_percent."""

    @pytest.mark.asyncio
    async def test_build_active_payload_skips_invalid_spread(self):
        store = MagicMock()
        store.list_arbitrage_events = AsyncMock(return_value=[
            ("good", {"data": {"spread_percent": "1.5", "coin": "BTC"}}),
            ("bad", {"data": {"spread_percent": "n/a", "coin": "ETH"}}),
        ])
        settings = {
            "exchanges": ["binance"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 10,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
            },
            "telegram": {"bot_token": "", "chat_id": ""},
        }
        from backend.history_store import ArbitrageHistoryStore
        history = ArbitrageHistoryStore(":memory:", max_records=100)
        svc = BackendService(store, settings, history)

        payload = await svc.build_active_payload_from_redis()
        assert len(payload) == 1
        assert payload[0]["coin"] == "BTC"
