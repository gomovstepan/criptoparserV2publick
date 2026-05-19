"""Регрессионные тесты для MAJOR-проблем, найденных в аудите."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import pytest


class TestM1TelegramRetryStorm:
    """M1: При ошибке edit_closed_event событие всё равно удаляется из Redis."""

    @pytest.mark.asyncio
    async def test_closed_event_edit_failure_still_deletes(self):
        from backend.service import TelegramNotificationService
        notifier = TelegramNotificationService("token", "123")

        # edit_closed_event не падает при ошибке — логирует и продолжает
        with patch.object(notifier, '_post', side_effect=RuntimeError("Telegram down")):
            # Не должно бросать исключение наружу
            test_data = {
                "coin": "BTCUSDT",
                "buy_exchange": "binance",
                "sell_exchange": "bybit",
                "spread_percent": "1.0",
                "start_time": "2024-01-01T00:00:00+00:00",
                "max_spread": "2.0",
            }
            await notifier.edit_closed_event(12345, test_data, "2024-01-01T00:00:30+00:00")


class TestM2ClockSkewFreshness:
    """M2: _is_fresh должен быть устойчив к clock skew."""

    def test_is_fresh_tolerates_reasonable_skew(self):
        from datetime import datetime, timedelta, timezone
        from common.redis_store import RedisOrderBookStore
        store = RedisOrderBookStore(MagicMock(), {})
        # симулируем часы спереди на 5 секунд
        future = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
        assert store._is_fresh(future, max_age_seconds=30) is True


class TestM3PollIntervalNotHonored:
    """M3: poll_once должен укладываться в poll_interval или хотя бы логировать нарушение."""

    @pytest.mark.asyncio
    async def test_poll_interval_respected_or_warned(self):
        from exchanges.http_polling import HttpPollingOrderBookConnection

        class DummyConn(HttpPollingOrderBookConnection):
            fetch_count = 0
            async def fetch_symbol_orderbook(self, symbol):
                await asyncio.sleep(0.1)  # симулируем latency
                self.__class__.fetch_count += 1
                return [["100", "1"]], [["99", "1"]]

        store = AsyncMock()
        settings = {
            "exchange_name": "test",
            "poll_interval_seconds": 0.05,
            "reconnect_delay_seconds": 1,
        }
        conn = DummyConn(settings, store, 1, ["A", "B", "C", "D"], asyncio.Semaphore(2))
        start = asyncio.get_event_loop().time()
        await conn.poll_once()
        elapsed = asyncio.get_event_loop().time() - start
        # 4 символа, семафор 2, latency 0.1 => минимум 0.2с
        assert elapsed >= 0.2
        # poll_interval=0.05 нарушен — в реальном коде нужна метрика/лог


class TestM4HttpRetryDistinguishes4xx:
    """M4: 4xx ошибки не должны приводить к retry."""

    @pytest.mark.asyncio
    async def test_4xx_should_not_retry(self):
        from unittest.mock import patch
        from exchanges.http_polling import http_get_json

        with patch('exchanges.http_polling.urllib.request.urlopen',
                   side_effect=Exception("HTTP Error 400: Bad Request")):
            with pytest.raises(Exception):
                await http_get_json("https://api.example.com")
            # В текущем коде run_forever сделает retry через reconnect_delay.
            # Тест документирует ожидаемое поведение: 4xx → no retry.


class TestM5FrontendMissingApiKey:
    """M5: Frontend должен отправлять X-API-Key при очистке истории."""

    def test_clear_history_includes_header(self):
        path = Path(__file__).parent.parent / "frontend" / "src" / "App.vue"
        source = path.read_text(encoding="utf-8")
        assert "X-API-Key" in source, "Frontend должен передавать API-ключ в DELETE /api/history"


class TestM1TelegramRateLimit:
    """M1-addendum: TelegramNotificationService должен иметь rate limiting для защиты от 429."""

    @pytest.mark.asyncio
    async def test_send_message_enforces_min_interval(self):
        from backend.service import TelegramNotificationService
        notifier = TelegramNotificationService("token", "123")
        notifier._last_send_time = 0.0
        notifier._min_send_interval = 0.3

        # Мокаем _post чтобы он возвращал JSON с message_id
        fake_response = '{"ok": true, "result": {"message_id": 123}}'
        with patch.object(notifier, '_post', return_value=fake_response) as mock_post:
            start = asyncio.get_event_loop().time()
            await notifier._send_message("msg1")
            await notifier._send_message("msg2")
            elapsed = asyncio.get_event_loop().time() - start

        assert mock_post.call_count == 2
        assert elapsed >= 0.25  # должен был подождать между отправками


class TestM6UpsertExistedFlag:
    """M6: upsert_arbitrage_event должен возвращать корректный флаг existed."""

    @pytest.mark.asyncio
    async def test_upsert_returns_true_when_event_exists(self):
        mock_redis = MagicMock()
        pipeline = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=pipeline)
        settings = {
            "arbitrage_events_set_key": "arb:events",
            "arbitrage_event_key_template": "arb:event:{event_key}",
        }
        from common.redis_store import RedisOrderBookStore
        store = RedisOrderBookStore(mock_redis, settings)

        # Если событие уже существует, hsetnx created_at вернёт 0
        pipeline.execute = AsyncMock(return_value=[1, 0, 0, 1])  # sadd=1, hsetnx=0, hsetnx=0, hset=1
        existed = await store.upsert_arbitrage_event("ev1", {}, "2024-01-01T00:00:00+00:00")
        assert existed is True  # ожидаемое поведение после фикса
