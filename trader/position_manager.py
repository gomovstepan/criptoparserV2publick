"""Управление арбитражными позициями — открытие, отслеживание, закрытие."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from trader.config import TraderConfig
from trader.models import (
    Position, PositionStatus, TradeExecution, TradeSide,
    TradeSignal, TradeType, SignalType,
)
from trader.exchange_clients.base import BaseExchangeClient
from trader.history_store import TradeHistoryStore


class PositionManager:
    """Открывает, отслеживает и закрывает арбитражные позиции."""

    def __init__(
        self,
        config: TraderConfig,
        exchange_clients: Dict[str, BaseExchangeClient],
        history_store: TradeHistoryStore,
    ):
        self.config = config
        self.clients = exchange_clients  # exchange_name -> client
        self.history = history_store
        self.logger = logging.getLogger("trader.position_manager")

        # Активные позиции: event_key -> Position
        self._active_positions: Dict[str, Position] = {}
        self._lock = asyncio.Lock()

    async def open_position(self, signal: TradeSignal) -> Optional[Position]:
        """Открывает новую арбитражную позицию по сигналу."""
        event_key = signal.event_key

        async with self._lock:
            if event_key in self._active_positions:
                self.logger.info("Позиция уже существует для %s", event_key)
                return None

        # Определяем тип торговли (demo / paper / real)
        trade_type = self._determine_trade_type(
            signal.buy_exchange, signal.sell_exchange
        )

        # Создаем объект позиции
        position = Position(
            id=str(uuid.uuid4()),
            event_key=event_key,
            coin=signal.coin,
            status=PositionStatus.OPENING,
            trade_type=trade_type,
            buy_exchange=signal.buy_exchange,
            sell_exchange=signal.sell_exchange,
            planned_buy_price=signal.buy_price,
            planned_sell_price=signal.sell_price,
            planned_spread=signal.spread_percent,
            planned_net_spread=signal.net_spread,
            confidence_at_entry=signal.confidence,
            target_value_usdt=signal.target_value_usdt,
            created_at=datetime.now(timezone.utc),
        )

        # Рассчитываем размер сделки
        trade_amount = await self._calculate_trade_amount(signal, trade_type)
        if trade_amount <= 0:
            self.logger.error(
                "Невозможно рассчитать размер сделки для %s", event_key,
            )
            position.status = PositionStatus.FAILED
            position.notes = "Недостаточно средств для сделки"
            self.history.save_position(position)
            return None

        # Сохраняем позицию в историю и кэш
        async with self._lock:
            self._active_positions[event_key] = position
        self.history.save_position(position)

        # Выполняем ордера: BUY на одной бирже, SELL на другой
        self.logger.info(
            "Открытие позиции %s: BUY %s на %s, SELL %s на %s, сумма=%s USDT",
            position.id, signal.coin, signal.buy_exchange,
            signal.coin, signal.sell_exchange, trade_amount,
        )

        # BUY ордер (покупаем монету дешевле)
        buy_client = self.clients.get(signal.buy_exchange)
        if buy_client:
            try:
                buy_trade = await buy_client.place_market_order(
                    signal.coin, TradeSide.BUY, trade_amount
                )
                position.buy_trade = buy_trade
                if buy_trade:
                    self.logger.info(
                        "BUY исполнен: %s @ %s USDT, fee=%s",
                        buy_trade.filled_amount, buy_trade.filled_price,
                        buy_trade.fee,
                    )
            except Exception as e:
                self.logger.error("Ошибка BUY ордера на %s: %s", signal.buy_exchange, e)

        # SELL ордер (продаем монету дороже)
        sell_client = self.clients.get(signal.sell_exchange)
        if sell_client:
            try:
                sell_trade = await sell_client.place_market_order(
                    signal.coin, TradeSide.SELL, trade_amount
                )
                position.sell_trade = sell_trade
                if sell_trade:
                    self.logger.info(
                        "SELL исполнен: %s @ %s USDT, fee=%s",
                        sell_trade.filled_amount, sell_trade.filled_price,
                        sell_trade.fee,
                    )
            except Exception as e:
                self.logger.error("Ошибка SELL ордера на %s: %s", signal.sell_exchange, e)

        # Обновляем статус позиции на основе исполнения
        if position.is_fully_executed:
            position.status = PositionStatus.OPEN
            position.opened_at = datetime.now(timezone.utc)
            self.logger.info(
                "Позиция %s ОТКРЫТА: buy@%s sell@%s",
                position.id,
                position.buy_trade.filled_price if position.buy_trade else "N/A",
                position.sell_trade.filled_price if position.sell_trade else "N/A",
            )
        else:
            position.status = PositionStatus.FAILED
            buy_ok = position.buy_trade is not None and position.buy_trade.filled_amount > 0
            sell_ok = position.sell_trade is not None and position.sell_trade.filled_amount > 0
            position.notes = f"Частичное исполнение: buy={buy_ok}, sell={sell_ok}"
            self.logger.error("Позиция %s ПРОВАЛЕНА: %s", position.id, position.notes)

        self.history.save_position(position)
        return position

    async def close_position(self, signal: TradeSignal) -> Optional[Position]:
        """Закрывает позицию обратной сделкой."""
        event_key = signal.event_key

        async with self._lock:
            position = self._active_positions.get(event_key)
            if not position:
                self.logger.info("Нет активной позиции для %s чтобы закрыть", event_key)
                return None

        position.status = PositionStatus.CLOSING
        position.closed_at = datetime.now(timezone.utc)

        # Обратная сделка:
        # Было: BUY на бирже A, SELL на бирже B
        # Теперь: SELL на бирже A (продать купленное), BUY на бирже B (купить проданное)
        self.logger.info(
            "Закрытие позиции %s: обратная сделка", position.id,
        )

        # SELL на бирже покупки — продаем то, что купили
        if position.buy_trade and position.buy_trade.filled_amount > 0:
            sell_client = self.clients.get(position.buy_exchange)
            if sell_client:
                try:
                    close_sell = await sell_client.place_market_order(
                        position.coin, TradeSide.SELL,
                        position.buy_trade.filled_amount * position.buy_trade.filled_price
                    )
                    if close_sell:
                        buy_cost = (position.buy_trade.filled_amount *
                                    position.buy_trade.filled_price)
                        sell_revenue = (close_sell.filled_amount *
                                        close_sell.filled_price - close_sell.fee)
                        position.calculated_pnl += sell_revenue - buy_cost
                        self.logger.info(
                            "Закрывающий SELL на %s: PnL=%s",
                            position.buy_exchange, sell_revenue - buy_cost,
                        )
                except Exception as e:
                    self.logger.error(
                        "Ошибка закрывающего SELL на %s: %s",
                        position.buy_exchange, e,
                    )

        # BUY на бирже продажи — покупаем то, что продали
        if position.sell_trade and position.sell_trade.filled_amount > 0:
            buy_client = self.clients.get(position.sell_exchange)
            if buy_client:
                try:
                    close_buy = await buy_client.place_market_order(
                        position.coin, TradeSide.BUY,
                        position.sell_trade.filled_amount * position.sell_trade.filled_price
                    )
                    if close_buy:
                        buy_back_cost = (close_buy.filled_amount *
                                         close_buy.filled_price + close_buy.fee)
                        sell_revenue_orig = (position.sell_trade.filled_amount *
                                             position.sell_trade.filled_price -
                                             position.sell_trade.fee)
                        position.calculated_pnl += sell_revenue_orig - buy_back_cost
                        self.logger.info(
                            "Закрывающий BUY на %s: PnL=%s",
                            position.sell_exchange, sell_revenue_orig - buy_back_cost,
                        )
                except Exception as e:
                    self.logger.error(
                        "Ошибка закрывающего BUY на %s: %s",
                        position.sell_exchange, e,
                    )

        # Финализируем позицию
        position.status = PositionStatus.CLOSED
        if position.buy_trade and position.sell_trade:
            position.calculated_pnl_percent = (
                (position.calculated_pnl / position.actual_buy_cost * Decimal("100"))
                if position.actual_buy_cost > 0 else Decimal("0")
            )

        self.logger.info(
            "Позиция %s ЗАКРЫТА: PnL=%s USDT (%.4f%%), длительность=%ss",
            position.id, position.actual_pnl, position.actual_pnl_percent,
            position.duration_seconds,
        )

        self.history.save_position(position)

        async with self._lock:
            del self._active_positions[event_key]

        return position

    def get_active_positions(self) -> List[Position]:
        """Возвращает список всех активных позиций."""
        return list(self._active_positions.values())

    def get_position(self, event_key: str) -> Optional[Position]:
        """Возвращает позицию по event_key."""
        return self._active_positions.get(event_key)

    def has_active_position(self, event_key: str) -> bool:
        """Проверяет есть ли активная позиция для event_key."""
        return event_key in self._active_positions

    async def _calculate_trade_amount(
        self,
        signal: TradeSignal,
        trade_type: TradeType,
    ) -> Decimal:
        """Рассчитывает размер сделки как % от доступного баланса (Q2: вариант Б)."""
        # Собираем USDT балансы обеих бирж
        balances = []
        for exchange_name in [signal.buy_exchange, signal.sell_exchange]:
            client = self.clients.get(exchange_name)
            if client:
                try:
                    bal = await client.get_balance("USDT")
                    if bal:
                        balances.append(bal.free)
                except Exception as e:
                    self.logger.error(
                        "Ошибка получения баланса %s: %s", exchange_name, e,
                    )

        if not balances:
            return Decimal("0")

        # Берем минимальный баланс между биржами для безопасности
        min_balance = min(balances)

        # Процент от баланса (по умолчанию 10%)
        trade_amount = (
            min_balance * self.config.trade_balance_percent / Decimal("100")
        )

        # Не меньше минимально допустимой суммы
        if trade_amount < self.config.min_trade_amount_usdt:
            return Decimal("0")

        # Не больше target_value из сигнала
        trade_amount = min(trade_amount, signal.target_value_usdt)

        return trade_amount.quantize(Decimal("0.0001"))

    def _determine_trade_type(
        self,
        buy_exchange: str,
        sell_exchange: str,
    ) -> TradeType:
        """Определяет тип торговли на основе режима и списка бирж."""
        if self.config.is_paper:
            return TradeType.PAPER

        if self.config.is_demo:
            # Если обе биржи в списке demo — demo режим
            demo_set = set(e.lower() for e in self.config.demo_exchanges)
            if (buy_exchange.lower() in demo_set and
                    sell_exchange.lower() in demo_set):
                return TradeType.DEMO
            # Если хотя бы одна биржа не в demo — безопасный paper режим
            return TradeType.PAPER

        if self.config.is_real:
            return TradeType.REAL

        # По умолчанию — самый безопасный режим
        return TradeType.PAPER
