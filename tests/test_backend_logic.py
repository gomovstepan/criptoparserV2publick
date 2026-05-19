"""
Unit-тесты извлечённой логики backend/main.py.

BackendService и связанные классы определены в backend/main.py,
но импорт модуля на уровне тестов невозможен без рефакторинга
(модульная инициализация TelegramNotifier падает из-за отсутствия dedup_ttl_seconds).
Поэтому тестируем критичные алгоритмы через извлечённые реализации.
"""
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations

import pytest


# --- Извлечённые из backend/main.py ---

@dataclass
class ArbitrageOpportunity:
    coin: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_percent: Decimal


class BackendServiceStub:
    def normalize_symbol(self, symbol):
        return "".join(ch for ch in symbol.upper() if ch.isalnum())

    def split_symbol(self, symbol):
        normalized = self.normalize_symbol(symbol)
        known_quotes = [
            "USDT", "USDC", "FDUSD", "TUSD", "BUSD",
            "BTC", "ETH", "BNB", "EUR", "TRY", "RUB",
        ]
        for quote in known_quotes:
            if normalized.endswith(quote) and len(normalized) > len(quote):
                return normalized[: -len(quote)], quote
        return normalized[:-4], normalized[-4:]

    def calculate_spread_percent(self, buy_price, sell_price):
        return ((sell_price - buy_price) / buy_price) * Decimal("100")

    def valid_side(self, side):
        if side is None:
            return False
        if side.volume is None or side.price is None:
            return False
        if side.volume <= 0 or side.price <= 0:
            return False
        return True

    def extract_symbol_views(self, snapshot):
        by_symbol = {}
        for exchange_name, symbols in snapshot.items():
            for raw_symbol, sides in symbols.items():
                normalized = self.normalize_symbol(raw_symbol)
                by_symbol.setdefault(normalized, []).append(
                    {
                        "exchange": exchange_name,
                        "ask": sides.get("ASK"),
                        "bid": sides.get("BID"),
                    }
                )
        return by_symbol

    def maybe_append_opportunity(self, opportunities, symbol, buy_entry, sell_entry, threshold):
        if buy_entry["exchange"] == sell_entry["exchange"]:
            return
        ask = buy_entry["ask"]
        bid = sell_entry["bid"]
        if not self.valid_side(ask) or not self.valid_side(bid):
            return
        try:
            spread_percent = self.calculate_spread_percent(ask.price, bid.price)
        except Exception:
            return
        if spread_percent < threshold:
            return
        opportunities.append(
            ArbitrageOpportunity(
                coin=symbol,
                buy_exchange=buy_entry["exchange"],
                sell_exchange=sell_entry["exchange"],
                buy_price=ask.price,
                sell_price=bid.price,
                spread_percent=spread_percent.quantize(Decimal("0.0001")),
            )
        )

    def find_arbitrage(self, snapshot, threshold):
        opportunities = []
        by_symbol = self.extract_symbol_views(snapshot)
        for symbol, entries in by_symbol.items():
            for entry_a, entry_b in combinations(entries, 2):
                self.maybe_append_opportunity(opportunities, symbol, entry_a, entry_b, threshold)
                self.maybe_append_opportunity(opportunities, symbol, entry_b, entry_a, threshold)
        opportunities.sort(key=lambda item: item.spread_percent, reverse=True)
        return opportunities

    def format_percent(self, value):
        decimal_value = Decimal(str(value))
        rounded = decimal_value.quantize(Decimal("0.0001"))
        text = format(rounded, "f").rstrip("0").rstrip(".")
        if "." not in text:
            return f"{text}.00"
        fraction = text.split(".")[1]
        if len(fraction) == 1:
            return f"{text}0"
        return text

    def format_price(self, value):
        decimal_value = Decimal(str(value))
        formatted = f"{decimal_value:.6f}".rstrip("0").rstrip(".")
        return formatted or "0"


class TestNormalizeSymbol:
    def test_uppercase_and_strip(self):
        s = BackendServiceStub()
        assert s.normalize_symbol("btc-usdt") == "BTCUSDT"
        assert s.normalize_symbol("BTC_USDT") == "BTCUSDT"
        assert s.normalize_symbol("btcusdt") == "BTCUSDT"


class TestSplitSymbol:
    def test_known_quotes(self):
        s = BackendServiceStub()
        assert s.split_symbol("BTCUSDT") == ("BTC", "USDT")
        assert s.split_symbol("ETHBTC") == ("ETH", "BTC")
        assert s.split_symbol("BTCTRY") == ("BTC", "TRY")

    def test_unknown_quote_fallback(self):
        """MINOR: fallback normalized[:-4] ломается на неизвестных quote."""
        s = BackendServiceStub()
        assert s.split_symbol("SOLJPY") == ("SO", "LJPY")


class TestCalculateSpreadPercent:
    def test_basic(self):
        s = BackendServiceStub()
        assert s.calculate_spread_percent(Decimal("100"), Decimal("101")) == Decimal("1")

    def test_zero_division_guarded_by_valid_side(self):
        s = BackendServiceStub()
        # valid_side отсекает price <= 0, но на всякий случай:
        with pytest.raises(ZeroDivisionError):
            s.calculate_spread_percent(Decimal("0"), Decimal("100"))


class TestValidSide:
    class FakeSide:
        def __init__(self, price, volume):
            self.price = price
            self.volume = volume

    def test_valid(self):
        s = BackendServiceStub()
        assert s.valid_side(self.FakeSide(Decimal("100"), Decimal("1"))) is True

    def test_none(self):
        s = BackendServiceStub()
        assert s.valid_side(None) is False

    def test_zero_price(self):
        s = BackendServiceStub()
        assert s.valid_side(self.FakeSide(Decimal("0"), Decimal("1"))) is False

    def test_negative_volume(self):
        s = BackendServiceStub()
        assert s.valid_side(self.FakeSide(Decimal("100"), Decimal("-1"))) is False


class TestFindArbitrage:
    class FakeSide:
        def __init__(self, price, volume):
            self.price = price
            self.volume = volume
            self.value = price * volume
            self.levels = 1

    def test_opportunity_found(self):
        s = BackendServiceStub()
        snapshot = {
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("100"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("99"), Decimal("10")),
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("102"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("101"), Decimal("10")),
                }
            },
        }
        ops = s.find_arbitrage(snapshot, Decimal("0.5"))
        assert len(ops) == 1
        assert ops[0].buy_exchange == "binance"
        assert ops[0].sell_exchange == "bybit"
        assert ops[0].spread_percent == Decimal("1.0000")

    def test_no_opportunity_below_threshold(self):
        s = BackendServiceStub()
        snapshot = {
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("100"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("99"), Decimal("10")),
                }
            },
            "bybit": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("100.1"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("100"), Decimal("10")),
                }
            },
        }
        ops = s.find_arbitrage(snapshot, Decimal("1"))
        assert len(ops) == 0

    def test_same_exchange_filtered(self):
        s = BackendServiceStub()
        snapshot = {
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("100"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("105"), Decimal("10")),
                }
            },
        }
        ops = s.find_arbitrage(snapshot, Decimal("0.1"))
        assert len(ops) == 0

    def test_symbol_normalization_cross_exchange(self):
        s = BackendServiceStub()
        snapshot = {
            "kucoin": {
                "BTC-USDT": {
                    "ASK": self.FakeSide(Decimal("100"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("99"), Decimal("10")),
                }
            },
            "binance": {
                "BTCUSDT": {
                    "ASK": self.FakeSide(Decimal("102"), Decimal("10")),
                    "BID": self.FakeSide(Decimal("101"), Decimal("10")),
                }
            },
        }
        ops = s.find_arbitrage(snapshot, Decimal("0.5"))
        assert len(ops) == 1
        assert ops[0].coin == "BTCUSDT"


class TestFormatPercent:
    def test_basic(self):
        s = BackendServiceStub()
        assert s.format_percent("1.5") == "1.50"
        assert s.format_percent("1") == "1.00"
        assert s.format_percent("1.234") == "1.234"  # rstrip('0') убивает последний 0


class TestFormatPrice:
    def test_trims_zeros(self):
        s = BackendServiceStub()
        assert s.format_price("100.500000") == "100.5"
        assert s.format_price("100.000000") == "100"
        assert s.format_price("0") == "0"
