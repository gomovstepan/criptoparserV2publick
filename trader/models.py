"""Модели данных для торгового модуля."""
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    """Тип торгового сигнала."""
    OPEN = "OPEN"
    CLOSE = "CLOSE"


class TradeSide(str, Enum):
    """Сторона сделки."""
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    """Статус позиции."""
    OPENING = "OPENING"      # Ордера размещены, ждем исполнения
    OPEN = "OPEN"            # Позиция полностью открыта
    CLOSING = "CLOSING"      # Ордера на закрытие размещены
    CLOSED = "CLOSED"        # Позиция закрыта
    FAILED = "FAILED"        # Ошибка при открытии/закрытии


class TradeType(str, Enum):
    """Тип торгового режима."""
    DEMO = "demo"            # Testnet/demo API
    PAPER = "paper"          # Внутренний paper trading
    REAL = "real"            # Реальная торговля


@dataclass
class TradeSignal:
    """Входящий торговый сигнал из Redis Pub/Sub."""
    signal_type: SignalType
    timestamp: datetime
    event_key: str
    coin: str
    buy_exchange: str
    sell_exchange: str
    buy_price: Decimal
    sell_price: Decimal
    spread_percent: Decimal
    net_spread: Decimal
    confidence: Decimal
    fee_total: Decimal
    target_value_usdt: Decimal

    @classmethod
    def from_dict(cls, data: dict) -> "TradeSignal":
        """Создает сигнал из словаря JSON."""
        return cls(
            signal_type=SignalType(data["signal_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            event_key=data["event_key"],
            coin=data["coin"],
            buy_exchange=data["buy_exchange"],
            sell_exchange=data["sell_exchange"],
            buy_price=Decimal(str(data["buy_price"])),
            sell_price=Decimal(str(data["sell_price"])),
            spread_percent=Decimal(str(data["spread_percent"])),
            net_spread=Decimal(str(data["net_spread"])),
            confidence=Decimal(str(data["confidence"])),
            fee_total=Decimal(str(data["fee_total"])),
            target_value_usdt=Decimal(str(data["target_value_usdt"])),
        )


@dataclass
class TradeExecution:
    """Результат исполнения одного ордера."""
    exchange: str
    side: TradeSide
    symbol: str
    amount: Decimal
    price: Decimal
    filled_amount: Decimal
    filled_price: Decimal
    fee: Decimal
    fee_currency: str
    order_id: str
    status: str
    timestamp: datetime
    is_simulated: bool = False  # True для paper trading


@dataclass
class Position:
    """Арбитражная позиция (купить на бирже А, продать на бирже Б)."""
    id: str                          # UUID позиции
    event_key: str                   # Ключ события (BTCUSDT:binance:bybit)
    coin: str
    status: PositionStatus
    trade_type: TradeType            # demo / paper / real

    # Параметры входа
    buy_exchange: str
    sell_exchange: str
    planned_buy_price: Decimal
    planned_sell_price: Decimal
    planned_spread: Decimal
    planned_net_spread: Decimal
    confidence_at_entry: Decimal
    target_value_usdt: Decimal

    # Фактическое исполнение
    buy_trade: Optional[TradeExecution] = None
    sell_trade: Optional[TradeExecution] = None

    # Время
    created_at: Optional[datetime] = None
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    # Расчетный PnL (для paper trading)
    calculated_pnl: Decimal = Decimal("0")
    calculated_pnl_percent: Decimal = Decimal("0")

    # Дополнительные поля
    notes: str = ""

    @property
    def duration_seconds(self) -> int:
        """Длительность позиции в секундах."""
        if self.opened_at and self.closed_at:
            return int((self.closed_at - self.opened_at).total_seconds())
        return 0

    @property
    def is_fully_executed(self) -> bool:
        """Оба ордера исполнены."""
        return (
            self.buy_trade is not None
            and self.sell_trade is not None
            and self.buy_trade.filled_amount > 0
            and self.sell_trade.filled_amount > 0
        )

    @property
    def actual_buy_cost(self) -> Decimal:
        """Фактическая стоимость покупки."""
        if self.buy_trade:
            return self.buy_trade.filled_amount * self.buy_trade.filled_price + self.buy_trade.fee
        return Decimal("0")

    @property
    def actual_sell_revenue(self) -> Decimal:
        """Фактическая выручка от продажи."""
        if self.sell_trade:
            return self.sell_trade.filled_amount * self.sell_trade.filled_price - self.sell_trade.fee
        return Decimal("0")

    @property
    def actual_pnl(self) -> Decimal:
        """Фактический PnL в USDT."""
        if not self.is_fully_executed:
            return self.calculated_pnl
        return self.actual_sell_revenue - self.actual_buy_cost

    @property
    def actual_pnl_percent(self) -> Decimal:
        """Фактический PnL в процентах."""
        if self.actual_buy_cost > 0:
            return (self.actual_pnl / self.actual_buy_cost) * Decimal("100")
        return Decimal("0")


@dataclass
class Balance:
    """Баланс на бирже."""
    exchange: str
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal
    timestamp: datetime


@dataclass
class RebalanceTransfer:
    """Планируемый перевод для ребалансировки."""
    from_exchange: str
    to_exchange: str
    asset: str
    amount: Decimal
    reason: str
    timestamp: datetime
