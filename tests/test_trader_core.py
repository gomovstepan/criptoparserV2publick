"""Тесты торгового модуля."""
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trader.models import (
    Balance, Position, PositionStatus, TradeExecution, TradeSide,
    TradeSignal, SignalType, TradeType, RebalanceTransfer,
)
from trader.config import TraderConfig
from trader.history_store import TradeHistoryStore
from trader.pnl_calculator import PnLCalculator


@pytest.fixture
def temp_db():
    """Временная база данных для тестов."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.fixture
def config():
    """Тестовая конфигурация."""
    return TraderConfig(
        trade_mode="paper",
        trade_balance_percent=Decimal("10"),
        min_trade_amount_usdt=Decimal("50"),
        confidence_threshold=Decimal("70"),
        min_net_spread=Decimal("0.3"),
        rebalance_enabled=True,
        rebalance_threshold_usdt=Decimal("1000"),
        rebalance_min_transfer_usdt=Decimal("100"),
        paper_balance_usdt=Decimal("10000"),
        history_db_path=":memory:",
    )


@pytest.fixture
def sample_signal():
    """Тестовый торговый сигнал."""
    return TradeSignal(
        signal_type=SignalType.OPEN,
        timestamp=datetime.now(timezone.utc),
        event_key="BTCUSDT:binance:bybit",
        coin="BTCUSDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        buy_price=Decimal("100000"),
        sell_price=Decimal("101000"),
        spread_percent=Decimal("1.0"),
        net_spread=Decimal("0.7"),
        confidence=Decimal("85"),
        fee_total=Decimal("0.2"),
        target_value_usdt=Decimal("5000"),
    )


class TestTradeSignal:
    """Тесты модели TradeSignal."""

    def test_from_dict(self):
        data = {
            "type": "trade_signal",
            "signal_type": "OPEN",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "event_key": "BTCUSDT:binance:bybit",
            "coin": "BTCUSDT",
            "buy_exchange": "binance",
            "sell_exchange": "bybit",
            "buy_price": "100000",
            "sell_price": "101000",
            "spread_percent": "1.0",
            "net_spread": "0.7",
            "confidence": "85",
            "fee_total": "0.2",
            "target_value_usdt": "5000",
        }
        signal = TradeSignal.from_dict(data)
        assert signal.coin == "BTCUSDT"
        assert signal.buy_exchange == "binance"
        assert signal.sell_exchange == "bybit"
        assert signal.spread_percent == Decimal("1.0")
        assert signal.net_spread == Decimal("0.7")
        assert signal.confidence == Decimal("85")


class TestPosition:
    """Тесты модели Position."""

    def test_actual_pnl_calculation(self, sample_signal):
        pos = Position(
            id="test-1",
            event_key=sample_signal.event_key,
            coin="BTCUSDT",
            status=PositionStatus.OPEN,
            trade_type=TradeType.PAPER,
            buy_exchange="binance",
            sell_exchange="bybit",
            planned_buy_price=Decimal("100000"),
            planned_sell_price=Decimal("101000"),
            planned_spread=Decimal("1.0"),
            planned_net_spread=Decimal("0.7"),
            confidence_at_entry=Decimal("85"),
            target_value_usdt=Decimal("5000"),
            buy_trade=TradeExecution(
                exchange="binance", side=TradeSide.BUY, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("100000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("100000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="b1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
            sell_trade=TradeExecution(
                exchange="bybit", side=TradeSide.SELL, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("101000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("101000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="s1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
            opened_at=datetime.now(timezone.utc),
        )
        # Buy cost: 0.05 * 100000 + 10 = 5010
        # Sell revenue: 0.05 * 101000 - 10 = 5040
        # PnL: 5040 - 5010 = 30
        assert pos.actual_buy_cost == Decimal("5010")
        assert pos.actual_sell_revenue == Decimal("5040")
        assert pos.actual_pnl == Decimal("30")

    def test_duration_seconds(self, sample_signal):
        now = datetime.now(timezone.utc)
        pos = Position(
            id="test-1", event_key="k", coin="BTCUSDT",
            status=PositionStatus.CLOSED, trade_type=TradeType.PAPER,
            buy_exchange="a", sell_exchange="b",
            planned_buy_price=Decimal("1"), planned_sell_price=Decimal("2"),
            planned_spread=Decimal("1"), planned_net_spread=Decimal("0.5"),
            confidence_at_entry=Decimal("80"), target_value_usdt=Decimal("1000"),
            opened_at=now,
            closed_at=now,
        )
        assert pos.duration_seconds == 0


class TestTradeHistoryStore:
    """Тесты хранилища истории."""

    def test_init_creates_tables(self, temp_db):
        store = TradeHistoryStore(temp_db)
        with sqlite3.connect(temp_db) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t[0] for t in tables}
        assert "positions" in table_names
        assert "balance_snapshots" in table_names
        assert "rebalance_operations" in table_names

    def test_save_and_get_position(self, temp_db, sample_signal):
        store = TradeHistoryStore(temp_db)
        pos = Position(
            id="test-1", event_key=sample_signal.event_key, coin="BTCUSDT",
            status=PositionStatus.CLOSED, trade_type=TradeType.PAPER,
            buy_exchange="binance", sell_exchange="bybit",
            planned_buy_price=Decimal("100000"), planned_sell_price=Decimal("101000"),
            planned_spread=Decimal("1.0"), planned_net_spread=Decimal("0.7"),
            confidence_at_entry=Decimal("85"), target_value_usdt=Decimal("5000"),
            created_at=datetime.now(timezone.utc),
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
            buy_trade=TradeExecution(
                exchange="binance", side=TradeSide.BUY, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("100000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("100000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="b1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
            sell_trade=TradeExecution(
                exchange="bybit", side=TradeSide.SELL, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("101000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("101000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="s1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
        )
        store.save_position(pos)
        positions = store.get_positions(status="CLOSED")
        assert len(positions) == 1
        assert positions[0]["id"] == "test-1"

    def test_pnl_summary(self, temp_db, sample_signal):
        store = TradeHistoryStore(temp_db)
        for i in range(3):
            pos = Position(
                id=f"p{i}", event_key=f"k{i}", coin="BTCUSDT",
                status=PositionStatus.CLOSED, trade_type=TradeType.PAPER,
                buy_exchange="binance", sell_exchange="bybit",
                planned_buy_price=Decimal("1"), planned_sell_price=Decimal("2"),
                planned_spread=Decimal("1"), planned_net_spread=Decimal("0.5"),
                confidence_at_entry=Decimal("80"), target_value_usdt=Decimal("1000"),
                created_at=datetime.now(timezone.utc),
                opened_at=datetime.now(timezone.utc),
                closed_at=datetime.now(timezone.utc),
            )
            store.save_position(pos)

        summary = store.get_pnl_summary(days=1)
        assert summary["total_trades"] == 3

    def test_save_balance_snapshot(self, temp_db):
        store = TradeHistoryStore(temp_db)
        store.save_balance_snapshot("binance", "USDT", Decimal("5000"), Decimal("5100"))
        with sqlite3.connect(temp_db) as conn:
            rows = conn.execute("SELECT * FROM balance_snapshots").fetchall()
        assert len(rows) == 1


class TestPnLCalculator:
    """Тесты калькулятора PnL."""

    def test_potential_pnl(self, config, sample_signal):
        calc = PnLCalculator(config, MagicMock())
        result = calc.calculate_potential_pnl(sample_signal, Decimal("5000"))

        # При spread 1%, fee 0.2%:
        # buy_cost = 5000, buy_fee = 10
        # sell_revenue = 5000 * 1.01 = 5050, sell_fee = 10.1
        # gross_pnl = 50, total_fees = 20.1, net_pnl ≈ 29.9
        assert result["investment"] == Decimal("5000")
        assert result["gross_pnl"] == Decimal("50")
        assert result["net_pnl"] > 0
        assert result["net_pnl_percent"] > 0

    def test_format_report(self, config):
        calc = PnLCalculator(config, MagicMock())
        report = {
            "date": "2024-01-15",
            "summary": {
                "total_trades": 10,
                "winning_trades": 7,
                "losing_trades": 3,
                "total_pnl": Decimal("150.50"),
                "avg_pnl_percent": Decimal("0.15"),
                "avg_duration_seconds": 45,
                "win_rate": 70.0,
            },
            "pnl_by_exchange": {"binance": "80.00", "bybit": "70.50"},
        }
        text = calc.format_report(report)
        assert "Всего сделок: 10" in text
        assert "Win rate: 70.0%" in text
        assert "binance: 80.00 USDT" in text

    def test_compare_potential_vs_actual(self, config, sample_signal):
        calc = PnLCalculator(config, MagicMock())
        pos = Position(
            id="test-1", event_key="k", coin="BTCUSDT",
            status=PositionStatus.CLOSED, trade_type=TradeType.PAPER,
            buy_exchange="a", sell_exchange="b",
            planned_buy_price=Decimal("100000"), planned_sell_price=Decimal("101000"),
            planned_spread=Decimal("1.0"), planned_net_spread=Decimal("0.7"),
            confidence_at_entry=Decimal("85"), target_value_usdt=Decimal("5000"),
            calculated_pnl=Decimal("25"),
            buy_trade=TradeExecution(
                exchange="binance", side=TradeSide.BUY, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("100000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("100000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="b1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
            sell_trade=TradeExecution(
                exchange="bybit", side=TradeSide.SELL, symbol="BTCUSDT",
                amount=Decimal("0.05"), price=Decimal("101000"),
                filled_amount=Decimal("0.05"), filled_price=Decimal("101000"),
                fee=Decimal("10"), fee_currency="USDT",
                order_id="s1", status="FILLED",
                timestamp=datetime.now(timezone.utc), is_simulated=True,
            ),
        )
        comparison = calc.compare_potential_vs_actual(pos)
        assert "potential_pnl" in comparison
        assert "actual_pnl" in comparison
        assert "slippage" in comparison
        assert "efficiency" in comparison


class TestTraderConfig:
    """Тесты конфигурации."""

    def test_defaults(self):
        c = TraderConfig()
        assert c.trade_mode == "demo"
        assert c.trade_balance_percent == Decimal("10")
        assert c.min_trade_amount_usdt == Decimal("50")
        assert c.confidence_threshold == Decimal("70")
        assert c.rebalance_threshold_usdt == Decimal("1000")

    def test_mode_flags(self):
        demo = TraderConfig(trade_mode="demo")
        paper = TraderConfig(trade_mode="paper")
        real = TraderConfig(trade_mode="real")

        assert demo.is_demo and not demo.is_paper and not demo.is_real
        assert paper.is_paper and not paper.is_demo and not paper.is_real
        assert real.is_real and not real.is_demo and not real.is_paper

    def test_get_exchange_credentials(self):
        c = TraderConfig(
            bybit_testnet_api_key="key1",
            bybit_testnet_api_secret="sec1",
            binance_testnet_api_key="key2",
            binance_testnet_api_secret="sec2",
        )
        bybit_creds = c.get_exchange_credentials("bybit")
        assert bybit_creds["api_key"] == "key1"
        assert bybit_creds["api_secret"] == "sec1"

        binance_creds = c.get_exchange_credentials("binance")
        assert binance_creds["api_key"] == "key2"


class TestBalance:
    """Тесты модели Balance."""

    def test_creation(self):
        bal = Balance(
            exchange="binance", asset="USDT",
            free=Decimal("5000"), locked=Decimal("100"),
            total=Decimal("5100"), timestamp=datetime.now(timezone.utc),
        )
        assert bal.exchange == "binance"
        assert bal.free == Decimal("5000")
        assert bal.total == Decimal("5100")
