"""Механизм автоматической ребалансировки балансов между биржами."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from trader.config import TraderConfig
from trader.models import RebalanceTransfer
from trader.balance_tracker import BalanceTracker
from trader.history_store import TradeHistoryStore


class RebalanceEngine:
    """Автоматическая ребалансировка USDT между биржами.

    Логика (Q3 — вариант В):
    - Периодически проверяет disparity балансов
    - Если max - min > threshold ($1000) → планирует перевод
    - Переводит с биржи с max на биржу с min
    """

    def __init__(
        self,
        config: TraderConfig,
        balance_tracker: BalanceTracker,
        history_store: TradeHistoryStore,
    ):
        self.config = config
        self.balances = balance_tracker
        self.history = history_store
        self.logger = logging.getLogger("trader.rebalance")
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Запускает периодические проверки ребаланса."""
        if not self.config.rebalance_enabled:
            self.logger.info("RebalanceEngine disabled")
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        self.logger.info(
            "RebalanceEngine started (threshold=%s USDT)",
            self.config.rebalance_threshold_usdt,
        )

    async def stop(self):
        """Останавливает проверки."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.logger.info("RebalanceEngine stopped")

    async def _check_loop(self):
        """Цикл проверки каждые N секунд."""
        while self._running:
            try:
                await self.check_and_rebalance()
            except Exception as e:
                self.logger.error("Rebalance check error: %s", e)
            await asyncio.sleep(self.config.rebalance_check_interval_seconds)

    async def check_and_rebalance(self):
        """Проверяет необходимость ребаланса и выполняет при необходимости."""
        disparity = self.balances.get_balance_disparity()
        if disparity < self.config.rebalance_threshold_usdt:
            return  # Не нужен ребаланс

        from_exchange = self.balances.get_exchange_with_max_balance()
        to_exchange = self.balances.get_exchange_with_min_balance()

        if not from_exchange or not to_exchange or from_exchange == to_exchange:
            return

        # Сумма перевода = половина disparity
        transfer_amount = disparity / Decimal("2")

        # Не меньше минимума
        if transfer_amount < self.config.rebalance_min_transfer_usdt:
            return

        self.logger.info(
            "Rebalance needed: disparity=%s USDT, %s → %s, amount=%s",
            disparity,
            from_exchange,
            to_exchange,
            transfer_amount,
        )

        # Выполняем перевод
        await self._execute_transfer(from_exchange, to_exchange, transfer_amount)

    async def _execute_transfer(self, from_ex: str, to_ex: str, amount: Decimal):
        """Выполняет перевод USDT между биржами.

        В demo/paper режиме — обновляет балансы напрямую.
        В реальном режиме — через API вывода (будет реализовано позже).
        """
        self.logger.info(
            "Executing rebalance transfer: %s USDT from %s to %s",
            amount,
            from_ex,
            to_ex,
        )

        # Получаем клиентов через balance_tracker
        from_client = self.balances.clients.get(from_ex)
        to_client = self.balances.clients.get(to_ex)

        if from_client and hasattr(from_client, "_balances"):
            # Paper / внутренний режим — меняем балансы напрямую
            from_client._balances["USDT"] = from_client._balances.get(
                "USDT", Decimal("0")
            ) - amount
            if hasattr(to_client, "_balances"):
                to_client._balances["USDT"] = to_client._balances.get(
                    "USDT", Decimal("0")
                ) + amount
            self.logger.info("Paper rebalance completed: %s → %s (%s USDT)", from_ex, to_ex, amount)
        else:
            # Режим с реальными API — логируем, требуется ручное подтверждение
            self.logger.warning(
                "Demo rebalance logged: %s → %s %s USDT. "
                "Real withdrawal requires API implementation.",
                from_ex,
                to_ex,
                amount,
            )

        # Фиксируем балансы до и после через историю
        from_balance = self.balances.get_usdt_by_exchange().get(from_ex, Decimal("0"))
        to_balance = self.balances.get_usdt_by_exchange().get(to_ex, Decimal("0"))

        self.history.save_balance_snapshot(from_ex, "USDT", from_balance, from_balance)
        self.history.save_balance_snapshot(to_ex, "USDT", to_balance, to_balance)

        transfer = RebalanceTransfer(
            from_exchange=from_ex,
            to_exchange=to_ex,
            asset="USDT",
            amount=amount,
            reason=f"Auto-rebalance: disparity > {self.config.rebalance_threshold_usdt} USDT",
            timestamp=datetime.now(timezone.utc),
        )
        self.logger.info("Rebalance transfer recorded: %s", transfer)
