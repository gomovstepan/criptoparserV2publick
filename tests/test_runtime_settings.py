"""
Тесты common/runtime_settings.py
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.runtime_settings import RuntimeSettingsStore, build_runtime_defaults, RUNTIME_SETTINGS_SCHEMA


class TestBuildRuntimeDefaults:
    def test_basic(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit,binance")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("ORDERBOOK_REDIS_KEY_PREFIX", "ob")
        monkeypatch.setenv("ORDERBOOK_REDIS_SYMBOLS_SET_TEMPLATE", "ob:{exchange}:symbols")
        monkeypatch.setenv("BACKEND_RENDER_INTERVAL_SECONDS", "0.5")
        monkeypatch.setenv("ORDERBOOK_TARGET_VALUE", "100")
        monkeypatch.setenv("ORDERBOOK_MAX_LEVELS", "4")
        monkeypatch.setenv("BYBIT_REST_ORDERBOOK_URL", "https://api.bybit.com/v5/market/orderbook")
        monkeypatch.setenv("BYBIT_SYMBOLS", "BTCUSDT")
        monkeypatch.setenv("BYBIT_ORDERBOOK_DEPTH", "50")
        monkeypatch.setenv("BYBIT_RECONNECT_DELAY_SECONDS", "2")
        monkeypatch.setenv("BYBIT_MAX_SYMBOLS_PER_CONNECTION", "10")
        monkeypatch.setenv("BINANCE_REST_ORDERBOOK_URL", "https://api.binance.com/api/v3/depth")
        monkeypatch.setenv("BINANCE_SYMBOLS", "BTCUSDT")
        monkeypatch.setenv("BINANCE_ORDERBOOK_DEPTH", "20")
        monkeypatch.setenv("BINANCE_RECONNECT_DELAY_SECONDS", "2")
        monkeypatch.setenv("BINANCE_MAX_SYMBOLS_PER_CONNECTION", "200")

        from common.config import load_settings
        settings = load_settings()
        defaults = build_runtime_defaults(settings)

        assert "fee_bybit_taker" in defaults
        assert "fee_binance_taker" in defaults
        assert defaults["tier_1_threshold"] == "0.30"


class TestRuntimeSettingsStore:
    @pytest.fixture
    def static_settings(self):
        return {
            "exchanges": ["bybit", "binance"],
            "backend": {
                "target_value": Decimal("800"),
                "max_levels": 4,
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "render_interval_seconds": 0.5,
                "confidence_min": Decimal("70"),
                "withdrawal_fee_usdt": Decimal("0"),
                "telegram_dedup_ttl_seconds": 60.0,
            },
            "fees": {
                "bybit": {"taker": Decimal("0.10")},
                "binance": {"taker": Decimal("0.10")},
            },
            "tiers": {
                "1": {"pairs": ["BTCUSDT"], "threshold": Decimal("0.30")},
            },
        }

    @pytest.fixture
    def store(self, static_settings, tmp_path):
        redis_client = AsyncMock()
        file_path = tmp_path / "runtime_settings.json"
        return RuntimeSettingsStore(
            redis_client=redis_client,
            redis_key="runtime:test",
            file_path=file_path,
            static_settings=static_settings,
        )

    @pytest.mark.asyncio
    async def test_load_from_redis(self, store):
        store.redis.hgetall = AsyncMock(return_value={
            b"tier_1_threshold": b"0.35",
            b"confidence_min": b"80",
        })
        loaded = await store.load()
        assert loaded["tier_1_threshold"] == "0.35"
        assert loaded["confidence_min"] == "80"
        assert loaded["tier_2_threshold"] == store.defaults["tier_2_threshold"]

    @pytest.mark.asyncio
    async def test_load_from_file_fallback(self, store, tmp_path):
        store.redis.hgetall = AsyncMock(return_value={})
        data = {"tier_1_threshold": "0.40", "confidence_min": "90"}
        with open(store.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        loaded = await store.load()
        assert loaded["tier_1_threshold"] == "0.40"
        assert loaded["confidence_min"] == "90"

    @pytest.mark.asyncio
    async def test_load_defaults_fallback(self, store):
        store.redis.hgetall = AsyncMock(return_value={})
        loaded = await store.load()
        assert loaded["tier_1_threshold"] == store.defaults["tier_1_threshold"]

    @pytest.mark.asyncio
    async def test_save_to_redis_and_file(self, store):
        payload = {"tier_1_threshold": "0.50", "confidence_min": "60"}
        await store.save(payload)
        store.redis.hset.assert_awaited_once()
        assert store.file_path.exists()
        with open(store.file_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["tier_1_threshold"] == "0.50"

    @pytest.mark.asyncio
    async def test_save_filters_unknown_keys(self, store):
        payload = {"tier_1_threshold": "0.50", "unknown_key": "123"}
        await store.save(payload)
        call_args = store.redis.hset.call_args
        mapping = call_args.kwargs.get("mapping") or call_args[1].get("mapping")
        assert "unknown_key" not in mapping

    def test_validate_ok(self, store):
        ok, errors = store.validate({"tier_1_threshold": "0.30", "confidence_min": "70"})
        assert ok is True
        assert errors == {}

    def test_validate_out_of_range(self, store):
        ok, errors = store.validate({"confidence_min": "150"})
        assert ok is False
        assert "confidence_min" in errors

    def test_validate_unknown_key(self, store):
        ok, errors = store.validate({"unknown": "123"})
        assert ok is False
        assert "unknown" in errors

    def test_get_schema(self, store):
        schema = store.get_schema()
        keys = {item["key"] for item in schema}
        assert "tier_1_threshold" in keys
        assert "exchanges" in keys
        for item in schema:
            if item["key"] == "exchanges":
                assert item["read_only"] is True
            if item["key"] == "tier_1_threshold":
                assert item["read_only"] is False
