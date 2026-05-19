"""
Тесты новой логики backend/service.py: tiers, confidence, net spread.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.service import BackendService
from backend.spread_calculator import NetSpreadCalculator


@pytest.fixture
def backend_service():
    settings = {
        "exchanges": ["binance", "bybit"],
        "backend": {
            "render_interval_seconds": 0.5,
            "target_value": Decimal("800"),
            "max_levels": 4,
            "arbitrage_min_spread_percent": Decimal("0.3"),
            "event_send_delay_seconds": 2.0,
            "event_expire_seconds": 8.0,
            "history_limit": 1000,
            "history_db_path": ":memory:",
            "confidence_min": Decimal("70"),
            "withdrawal_fee_usdt": Decimal("0"),
        },
        "telegram": {"bot_token": "", "chat_id": ""},
        "fees": {
            "binance": {"taker": Decimal("0.10")},
            "bybit": {"taker": Decimal("0.10")},
        },
        "tiers": {
            "1": {"pairs": ["BTCUSDT"], "threshold": Decimal("0.25")},
            "2": {"pairs": ["SOLUSDT"], "threshold": Decimal("0.35")},
        },
    }
    store = MagicMock()
    history_store = MagicMock()
    service = BackendService(store, settings, history_store)
    return service


class TestTierThreshold:
    def test_tier_1_threshold(self, backend_service):
        assert backend_service.get_threshold_for_symbol("BTCUSDT") == Decimal("0.25")

    def test_tier_2_threshold(self, backend_service):
        assert backend_service.get_threshold_for_symbol("SOLUSDT") == Decimal("0.35")

    def test_unknown_symbol_uses_default(self, backend_service):
        assert backend_service.get_threshold_for_symbol("UNKNOWN") == Decimal("0.3")


class TestCalculateConfidence:
    def test_full_confidence(self, backend_service):
        score = backend_service.calculate_confidence(
            data_age_ms=100,
            liquidity_exhausted=False,
            target_value=Decimal("800"),
            actual_quote_value=Decimal("800"),
        )
        # 40 (fresh) + 30 (liquidity) + 30 (depth_ratio=1) = 100
        assert score == Decimal("100.0")

    def test_high_confidence_with_depth(self, backend_service):
        score = backend_service.calculate_confidence(
            data_age_ms=100,
            liquidity_exhausted=False,
            target_value=Decimal("800"),
            actual_quote_value=Decimal("1000"),
        )
        # 40 (fresh) + 30 (liquidity) + 24 (depth_ratio=0.8) = 94
        assert score == Decimal("94.0")

    def test_low_confidence_old_data(self, backend_service):
        score = backend_service.calculate_confidence(
            data_age_ms=5000,
            liquidity_exhausted=False,
            target_value=Decimal("800"),
            actual_quote_value=Decimal("1000"),
        )
        # 0 (old) + 30 (liquidity) + 24 (depth_ratio=0.8) = 54
        assert score == Decimal("54.0")

    def test_zero_confidence_exhausted_old(self, backend_service):
        score = backend_service.calculate_confidence(
            data_age_ms=5000,
            liquidity_exhausted=True,
            target_value=Decimal("800"),
            actual_quote_value=Decimal("100"),
        )
        # 0 (old) + 0 (exhausted) + 30 (depth_ratio capped at 1) = 30
        assert score == Decimal("30.0")


class TestFindArbitrageWithNetSpread:
    class FakeSide:
        def __init__(self, price, volume, value=None, levels=1):
            self.price = Decimal(str(price))
            self.volume = Decimal(str(volume))
            self.value = value if value is not None else self.price * self.volume
            self.levels = levels

    def test_opportunity_includes_net_spread(self):
        settings = {
            "exchanges": ["binance", "bybit"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 4,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
                "confidence_min": Decimal("70"),
                "withdrawal_fee_usdt": Decimal("0"),
            },
            "telegram": {"bot_token": "", "chat_id": ""},
            "fees": {
                "binance": {"taker": Decimal("0.001")},
                "bybit": {"taker": Decimal("0.001")},
            },
            "tiers": {},
        }
        service = BackendService(MagicMock(), settings, MagicMock())

        aggregated = {
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide("100", "10"),
                    "BID": self.FakeSide("99", "10"),
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": self.FakeSide("102", "10"),
                    "BID": self.FakeSide("101", "10"),
                }
            },
        }
        raw = {
            "binance": {
                "BTCUSDT": {
                    "ASK": [{"price": Decimal("100"), "volume": Decimal("10")}],
                    "BID": [{"price": Decimal("99"), "volume": Decimal("10")}],
                    "_meta": {"updated_at": "2024-01-01T00:00:00+00:00"},
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": [{"price": Decimal("102"), "volume": Decimal("10")}],
                    "BID": [{"price": Decimal("101"), "volume": Decimal("10")}],
                    "_meta": {"updated_at": "2024-01-01T00:00:00+00:00"},
                }
            },
        }
        opportunities = service.find_arbitrage(aggregated, raw)
        assert len(opportunities) == 1
        opp = opportunities[0]
        assert opp.spread_percent == Decimal("1.0000")
        assert opp.net_spread < opp.spread_percent  # net = gross - fees
        assert opp.confidence > Decimal("0")

    def test_no_opportunity_below_tier_threshold(self):
        settings = {
            "exchanges": ["binance", "bybit"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 4,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
                "confidence_min": Decimal("70"),
                "withdrawal_fee_usdt": Decimal("0"),
            },
            "telegram": {"bot_token": "", "chat_id": ""},
            "fees": {},
            "tiers": {
                "1": {"pairs": ["BTCUSDT"], "threshold": Decimal("1.0")},
            },
        }
        service = BackendService(MagicMock(), settings, MagicMock())

        aggregated = {
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide("100", "10"),
                    "BID": self.FakeSide("99", "10"),
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": self.FakeSide("100.5", "10"),
                    "BID": self.FakeSide("100.2", "10"),
                }
            },
        }
        raw = {
            "binance": {
                "BTCUSDT": {
                    "ASK": [{"price": Decimal("100"), "volume": Decimal("10")}],
                    "BID": [{"price": Decimal("99"), "volume": Decimal("10")}],
                    "_meta": {"updated_at": "2024-01-01T00:00:00+00:00"},
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": [{"price": Decimal("100.5"), "volume": Decimal("10")}],
                    "BID": [{"price": Decimal("100.2"), "volume": Decimal("10")}],
                    "_meta": {"updated_at": "2024-01-01T00:00:00+00:00"},
                }
            },
        }
        opportunities = service.find_arbitrage(aggregated, raw)
        assert len(opportunities) == 0  # spread 0.2% < tier threshold 1.0%


class TestRebuildFeeConfigFromRuntime:
    def test_runtime_fee_override(self):
        settings = {
            "exchanges": ["binance"],
            "backend": {
                "render_interval_seconds": 0.5,
                "target_value": Decimal("100"),
                "max_levels": 4,
                "arbitrage_min_spread_percent": Decimal("0.3"),
                "event_send_delay_seconds": 2.0,
                "event_expire_seconds": 8.0,
                "history_limit": 1000,
                "history_db_path": ":memory:",
                "confidence_min": Decimal("70"),
                "withdrawal_fee_usdt": Decimal("0"),
            },
            "telegram": {"bot_token": "", "chat_id": ""},
            "fees": {"binance": {"taker": Decimal("0.10")}},
            "tiers": {},
        }
        service = BackendService(MagicMock(), settings, MagicMock())
        service._runtime_settings = {"fee_binance_taker": "0.05"}
        service._rebuild_fee_config_from_runtime()
        assert service.spread_calculator.fee_config["binance"]["taker"] == Decimal("0.05")
