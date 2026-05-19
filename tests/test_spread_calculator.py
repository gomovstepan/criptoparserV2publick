"""
Unit-тесты backend/spread_calculator.py
"""
from decimal import Decimal

import pytest

from backend.spread_calculator import NetSpreadCalculator, VWAPResult


class TestCalculateVWAP:
    def test_basic_ask(self):
        calc = NetSpreadCalculator({})
        levels = [
            {"price": Decimal("100"), "volume": Decimal("5")},
            {"price": Decimal("101"), "volume": Decimal("10")},
        ]
        result = calc.calculate_vwap(levels, Decimal("500"))
        assert result.liquidity_exhausted is False
        assert result.vwap == Decimal("100")
        assert result.slippage_pct == Decimal("0")

    def test_slippage_when_deep(self):
        calc = NetSpreadCalculator({})
        levels = [
            {"price": Decimal("100"), "volume": Decimal("1")},
            {"price": Decimal("101"), "volume": Decimal("10")},
        ]
        result = calc.calculate_vwap(levels, Decimal("150"))
        # 1*100 + 0.495*101 = 149.995; base = 1.495; vwap = 100.3311
        assert result.liquidity_exhausted is False
        assert result.slippage_pct > Decimal("0")

    def test_liquidity_exhausted(self):
        calc = NetSpreadCalculator({})
        levels = [
            {"price": Decimal("100"), "volume": Decimal("1")},
        ]
        result = calc.calculate_vwap(levels, Decimal("500"))
        assert result.liquidity_exhausted is True

    def test_empty_orderbook(self):
        calc = NetSpreadCalculator({})
        result = calc.calculate_vwap([], Decimal("100"))
        assert result.liquidity_exhausted is True
        assert result.vwap == Decimal("0")

    def test_bid_slippage_positive(self):
        calc = NetSpreadCalculator({})
        levels = [
            {"price": Decimal("101"), "volume": Decimal("5")},
            {"price": Decimal("100"), "volume": Decimal("10")},
        ]
        result = calc.calculate_vwap(levels, Decimal("500"))
        # slippage для bid должно быть положительным
        assert result.slippage_pct >= Decimal("0")


class TestCalculateNetSpread:
    def test_no_fees_no_slippage(self):
        calc = NetSpreadCalculator(
            {"binance": {"taker": Decimal("0")}, "bybit": {"taker": Decimal("0")}}
        )
        asks = [{"price": Decimal("100"), "volume": Decimal("100")}]
        bids = [{"price": Decimal("101"), "volume": Decimal("100")}]
        result = calc.calculate_net_spread(
            gross_spread_pct=Decimal("1"),
            buy_exchange="binance",
            sell_exchange="bybit",
            orderbook_buy_asks=asks,
            orderbook_sell_bids=bids,
            target_quote_value=Decimal("100"),
        )
        assert result.gross_spread == Decimal("1.0000")
        assert result.net_spread == Decimal("1.0000")
        assert result.is_profitable is True

    def test_with_fees(self):
        calc = NetSpreadCalculator(
            {"binance": {"taker": Decimal("0.10")}, "bybit": {"taker": Decimal("0.10")}}
        )
        asks = [{"price": Decimal("100"), "volume": Decimal("100")}]
        bids = [{"price": Decimal("101"), "volume": Decimal("100")}]
        result = calc.calculate_net_spread(
            gross_spread_pct=Decimal("1"),
            buy_exchange="binance",
            sell_exchange="bybit",
            orderbook_buy_asks=asks,
            orderbook_sell_bids=bids,
            target_quote_value=Decimal("100"),
        )
        # fee_total = 0.2%
        assert result.fee_total == Decimal("0.2000")
        assert result.net_spread == Decimal("0.8000")

    def test_unprofitable_after_fees(self):
        calc = NetSpreadCalculator(
            {"binance": {"taker": Decimal("0.10")}, "bybit": {"taker": Decimal("0.10")}}
        )
        asks = [{"price": Decimal("100"), "volume": Decimal("100")}]
        bids = [{"price": Decimal("100.1"), "volume": Decimal("100")}]
        result = calc.calculate_net_spread(
            gross_spread_pct=Decimal("0.1"),
            buy_exchange="binance",
            sell_exchange="bybit",
            orderbook_buy_asks=asks,
            orderbook_sell_bids=bids,
            target_quote_value=Decimal("100"),
        )
        assert result.is_profitable is False
        assert result.net_spread < Decimal("0")

    def test_withdrawal_fee(self):
        calc = NetSpreadCalculator({}, withdrawal_fee_usdt=Decimal("1"))
        asks = [{"price": Decimal("100"), "volume": Decimal("100")}]
        bids = [{"price": Decimal("101"), "volume": Decimal("100")}]
        result = calc.calculate_net_spread(
            gross_spread_pct=Decimal("1"),
            buy_exchange="binance",
            sell_exchange="bybit",
            orderbook_buy_asks=asks,
            orderbook_sell_bids=bids,
            target_quote_value=Decimal("1000"),
        )
        # withdrawal_pct = 1 / 1000 * 100 = 0.1%
        assert result.withdrawal_pct == Decimal("0.1000")

    def test_token_discount(self):
        calc = NetSpreadCalculator(
            {"binance": {"taker": Decimal("0.10"), "token_discount": Decimal("0.25")}}
        )
        asks = [{"price": Decimal("100"), "volume": Decimal("100")}]
        bids = [{"price": Decimal("101"), "volume": Decimal("100")}]
        result = calc.calculate_net_spread(
            gross_spread_pct=Decimal("1"),
            buy_exchange="binance",
            sell_exchange="bybit",
            orderbook_buy_asks=asks,
            orderbook_sell_bids=bids,
            target_quote_value=Decimal("100"),
        )
        # fee_buy = 0.075%, fee_sell = default 0.1%
        assert result.fee_total == Decimal("0.1750")


class TestReloadFees:
    def test_reload_updates_config(self):
        calc = NetSpreadCalculator({"binance": {"taker": Decimal("0.001")}})
        calc.reload_fees({"binance": {"taker": Decimal("0.002")}})
        assert calc.fee_config["binance"]["taker"] == Decimal("0.002")
