"""
Тесты common/redis_store.py — DAL для Redis и агрегации стакана.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import json

import pytest

from common.redis_store import AggregatedSide, RedisOrderBookStore


class TestDecodeBook:
    def test_decodes_bytes_and_strings(self):
        store = RedisOrderBookStore(MagicMock(), {})
        raw = {b"100.5": b"10", "101.0": "5"}
        book = store.decode_book(raw)
        assert book == {Decimal("100.5"): Decimal("10"), Decimal("101.0"): Decimal("5")}

    def test_skips_invalid(self):
        store = RedisOrderBookStore(MagicMock(), {})
        raw = {"bad": "10", "100.5": "bad", "101.0": "5"}
        book = store.decode_book(raw)
        assert book == {Decimal("101.0"): Decimal("5")}


class TestDecodeLevels:
    def test_ask_sorts_ascending(self):
        store = RedisOrderBookStore(MagicMock(), {})
        raw = {b"101.0": b"5", b"100.5": b"10"}
        levels = store._decode_levels(raw, "ASK")
        assert [l["price"] for l in levels] == [Decimal("100.5"), Decimal("101.0")]

    def test_bid_sorts_descending(self):
        store = RedisOrderBookStore(MagicMock(), {})
        raw = {b"100.5": b"10", b"101.0": b"5"}
        levels = store._decode_levels(raw, "BID")
        assert [l["price"] for l in levels] == [Decimal("101.0"), Decimal("100.5")]


class TestAggregateSide:
    def test_target_value_reached(self):
        store = RedisOrderBookStore(MagicMock(), {})
        book = {Decimal("100"): Decimal("5"), Decimal("101"): Decimal("10")}
        side = store.aggregate_side(book, "ASK", Decimal("800"), 10)
        assert side is not None
        assert side.value >= Decimal("800")
        assert side.price == Decimal("100.6666666666666666666666667")

    def test_not_enough_value_returns_none(self):
        store = RedisOrderBookStore(MagicMock(), {})
        book = {Decimal("100"): Decimal("1")}
        side = store.aggregate_side(book, "ASK", Decimal("1000"), 10)
        assert side is None

    def test_max_levels_limit(self):
        store = RedisOrderBookStore(MagicMock(), {})
        book = {Decimal(str(i)): Decimal("2") for i in range(1, 100)}
        side = store.aggregate_side(book, "ASK", Decimal("22"), 5)
        assert side is not None
        assert side.levels == 5

    def test_empty_book_returns_none(self):
        store = RedisOrderBookStore(MagicMock(), {})
        assert store.aggregate_side({}, "ASK", Decimal("100"), 10) is None


class TestWriteOrderbook:
    @pytest.mark.asyncio
    async def test_pipeline_with_transaction(self):
        """
        write_orderbook использует pipeline(transaction=True) для атомарности.
        """
        mock_redis = MagicMock()
        mock_pipeline = AsyncMock()
        mock_redis.pipeline.return_value = mock_pipeline
        settings = {
            "key_prefix": "ob",
            "symbols_set_template": "ob:{exchange}:symbols",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        await store.write_orderbook("binance", "BTCUSDT", "snapshot", [["100", "1"]], [["99", "2"]])

        mock_redis.pipeline.assert_called_once()
        assert mock_redis.pipeline.call_args.kwargs.get("transaction") is False
        mock_pipeline.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ttl_set(self):
        mock_redis = MagicMock()
        mock_pipeline = AsyncMock()
        mock_redis.pipeline.return_value = mock_pipeline
        settings = {
            "key_prefix": "ob",
            "symbols_set_template": "ob:{exchange}:symbols",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        await store.write_orderbook("binance", "BTCUSDT", "snapshot", [["100", "1"]], [["99", "2"]])

        # TTL = 60 секунд на ask, bid и meta
        calls = [call for call in mock_pipeline.method_calls if "expire" in str(call)]
        assert len(calls) == 3


class TestReadRawSnapshot:
    @pytest.mark.asyncio
    async def test_graceful_degradation_on_single_exchange_failure(self):
        """
        При ошибке одной биржи read_raw_snapshot должен продолжать с остальными.
        """
        mock_redis = AsyncMock()
        mock_redis.smembers.side_effect = Exception("Redis down")
        settings = {
            "symbols_set_template": "ob:{exchange}:symbols",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        result = await store.read_raw_snapshot(["binance", "bybit"])
        assert result == {"binance": {}, "bybit": {}}


class TestListArbitrageEvents:
    @pytest.mark.asyncio
    async def test_corrupted_event_json_is_skipped(self):
        """
        Повреждённое событие в Redis должно пропускаться, а не ломать весь payload.
        """
        mock_redis = AsyncMock()
        mock_redis.smembers = AsyncMock(return_value={b"event1"})
        pipeline = MagicMock()
        pipeline.hgetall.return_value = None  # redis-py pipeline returns self/None for chaining
        pipeline.execute = AsyncMock(return_value=[{b"sent": b"0", b"data": b"{bad json"}])
        mock_redis.pipeline = MagicMock(return_value=pipeline)

        settings = {
            "arbitrage_events_set_key": "arb:events",
            "arbitrage_event_key_template": "arb:event:{event_key}",
        }
        store = RedisOrderBookStore(mock_redis, settings)

        events = await store.list_arbitrage_events()
        assert events == []
