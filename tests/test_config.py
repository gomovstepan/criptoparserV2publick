"""
Тесты common/config.py
"""
import os
from decimal import Decimal

import pytest

from common.config import get_env, load_settings, parse_csv


class TestParseCsv:
    def test_basic(self):
        assert parse_csv("a,b,c") == ["a", "b", "c"]

    def test_spaces_trimmed(self):
        assert parse_csv(" a , b , c ") == ["a", "b", "c"]

    def test_empty_items_skipped(self):
        assert parse_csv("a,,c") == ["a", "c"]


class TestGetEnv:
    def test_required_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(RuntimeError, match="MISSING_VAR"):
            get_env("MISSING_VAR", required=True)

    def test_default_used(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert get_env("MISSING_VAR", default="fallback") == "fallback"


class TestLoadSettings:
    def test_minimal_env(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
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

        settings = load_settings()
        assert settings["exchanges"] == ["bybit"]
        assert settings["backend"]["target_value"] == Decimal("100")
        assert settings["backend"]["arbitrage_min_spread_percent"] == Decimal("0.3")

    def test_arbitrage_min_spread_from_legacy_var(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        monkeypatch.setenv("ORDERBOOK_REDIS_KEY_PREFIX", "ob")
        monkeypatch.setenv("ORDERBOOK_REDIS_SYMBOLS_SET_TEMPLATE", "ob:{exchange}:symbols")
        monkeypatch.setenv("BACKEND_RENDER_INTERVAL_SECONDS", "0.5")
        monkeypatch.setenv("ORDERBOOK_TARGET_VALUE", "100")
        monkeypatch.setenv("ORDERBOOK_MAX_LEVELS", "4")
        monkeypatch.delenv("ARBITRAGE_MIN_SPREAD_PERCENT", raising=False)
        monkeypatch.setenv("SPREAD_THRESHOLD_PCT", "1.5")
        monkeypatch.setenv("BYBIT_REST_ORDERBOOK_URL", "https://api.bybit.com/v5/market/orderbook")
        monkeypatch.setenv("BYBIT_SYMBOLS", "BTCUSDT")
        monkeypatch.setenv("BYBIT_ORDERBOOK_DEPTH", "50")
        monkeypatch.setenv("BYBIT_RECONNECT_DELAY_SECONDS", "2")
        monkeypatch.setenv("BYBIT_MAX_SYMBOLS_PER_CONNECTION", "10")

        settings = load_settings()
        assert settings["backend"]["arbitrage_min_spread_percent"] == Decimal("1.5")

    def test_max_concurrent_requests_default(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
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
        monkeypatch.delenv("BYBIT_MAX_CONCURRENT_REQUESTS", raising=False)

        settings = load_settings()
        assert settings["exchange_configs"]["bybit"]["max_concurrent_requests"] == 10

    def test_max_concurrent_requests_custom(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
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
        monkeypatch.setenv("BYBIT_MAX_CONCURRENT_REQUESTS", "25")

        settings = load_settings()
        assert settings["exchange_configs"]["bybit"]["max_concurrent_requests"] == 25

    def test_api_key_default_empty(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
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

        settings = load_settings()
        assert settings["api_key"] == ""

    def test_api_key_custom(self, monkeypatch):
        monkeypatch.setenv("EXCHANGES", "bybit")
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
        monkeypatch.setenv("API_KEY", "secret123")

        settings = load_settings()
        assert settings["api_key"] == "secret123"
