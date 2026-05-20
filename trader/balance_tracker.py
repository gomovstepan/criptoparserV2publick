"""Отслеживание балансов на всех биржах."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from trader.config import TraderConfig
from trader.models import Balance
from trader.exchange_clients.base import BaseExchangeClient
from trader.history_store import TradeHistoryStore


class BalanceTracker:
    """Периодически опрашивает балансы на всех биржах и сохраняет историю."""

    def __init__(
        self,
        config: TraderConfig,
        exchange_clients: Dict[str, BaseExchangeClient],
        history_store: TradeHistoryStore,
    ):
        self.config = config
        self.clients = exchange_clients
        self.history = history_store
        self.logger = logging.getLogger("trader.balance_tracker")
        # Кэш балансов: exchange -> asset -> Balance
        self._balances: Dict[str, Dict[str, Balance]] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Запускает периодическое обновление балансов."""
        self._running = True
        # Первое обновление сразу
        await self.refresh_all()
        # Запускаем фоновый цикл
        self._task = asyncio.create_task(
            self._update_loop(), name="balance_tracker_loop"
        )
        self.logger.info("BalanceTracker запущен")

    async def stop(self):
        """Останавливает обновление балансов."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("BalanceTracker остановлен")

    async def _update_loop(self):
        """Цикл обновления балансов каждые 30 секунд."""
        while self._running:
            try:
                await self.refresh_all()
            except Exception as e:
                self.logger.error("Ошибка обновления балансов: %s", e)
            await asyncio.sleep(30)

    async def refresh_all(self):
        """Обновляет балансы всех подключенных бирж."""
        for exchange_name, client in self.clients.items():
            try:
                # Запрашиваем USDT баланс (основная торговая валюта)
                usdt_balance = await client.get_balance("USDT")
                if usdt_balance:
                    self._set_balance(exchange_name, "USDT", usdt_balance)
                    self.history.save_balance_snapshot(
                        exchange_name, "USDT",
                        usdt_balance.free, usdt_balance.total,
                    )
                    self.logger.debug(
                        "Баланс %s USDT: free=%s total=%s",
                        exchange_name, usdt_balance.free, usdt_balance.total,
                    )

                # Если paper client — получаем все балансы
                if hasattr(client, 'get_all_balances'):
                    try:
                        all_bal = await client.get_all_balances()
                        for asset, amount in all_bal.items():
                            if asset != "USDT" and amount > 0:
                                bal = Balance(
                                    exchange=exchange_name,
                                    asset=asset,
                                    free=amount,
                                    locked=Decimal("0"),
                                    total=amount,
                                    timestamp=datetime.now(timezone.utc),
                                )
                                self._set_balance(exchange_name, asset, bal)
                    except Exception as e:
                        self.logger.error(
                            "Ошибка получения всех балансов %s: %s",
                            exchange_name, e,
                        )

            except Exception as e:
                self.logger.error(
                    "Не удалось обновить баланс для %s: %s", exchange_name, e,
                )

    def _set_balance(self, exchange: str, asset: str, balance: Balance):
        """Сохраняет баланс в кэш."""
        if exchange not in self._balances:
            self._balances[exchange] = {}
        self._balances[exchange][asset] = balance

    def get_balance(self, exchange: str, asset: str = "USDT") -> Optional[Balance]:
        """Возвращает баланс из кэша."""
        return self._balances.get(exchange, {}).get(asset)

    def get_all_balances(self) -> Dict[str, Dict[str, Balance]]:
        """Возвращает все балансы всех бирж."""
        return self._balances

    def get_total_usdt(self) -> Decimal:
        """Суммарный USDT баланс на всех биржах."""
        total = Decimal("0")
        for exchange_balances in self._balances.values():
            usdt = exchange_balances.get("USDT")
            if usdt:
                total += usdt.free
        return total

    def get_usdt_by_exchange(self) -> Dict[str, Decimal]:
        """Возвращает USDT баланс по каждой бирже."""
        result = {}
        for exchange, balances in self._balances.items():
            usdt = balances.get("USDT")
            result[exchange] = usdt.free if usdt else Decimal("0")
        return result

    def get_balance_disparity(self) -> Decimal:
        """Максимальная разница между балансами бирж (для ребалансировки Q3)."""
        amounts = list(self.get_usdt_by_exchange().values())
        if len(amounts) < 2:
            return Decimal("0")
        return max(amounts) - min(amounts)

    def get_exchange_with_max_balance(self) -> Optional[str]:
        """Возвращает биржу с максимальным USDT балансом."""
        by_exchange = self.get_usdt_by_exchange()
        if not by_exchange:
            return None
        return max(by_exchange, key=by_exchange.get)

    def get_exchange_with_min_balance(self) -> Optional[str]:
        """Возвращает биржу с минимальным USDT балансом."""
        by_exchange = self.get_usdt_by_exchange()
        if not by_exchange:
            return None
        return min(by_exchange, key=by_exchange.get)

    def is_rebalance_needed(self) -> bool:
        """Проверяет необходимость ребалансировки (Q3: вариант В)."""
        if not self.config.rebalance_enabled:
            return False
        disparity = self.get_balance_disparity()
        return disparity >= self.config.rebalance_threshold_usdt

    def get_rebalance_recommendation(self) -> Optional[Dict]:
        """Возвращает рекомендацию по ребалансировке."""
        if not self.is_rebalance_needed():
            return None

        from_ex = self.get_exchange_with_max_balance()
        to_ex = self.get_exchange_with_min_balance()
        disparity = self.get_balance_disparity()

        # Переводим половину разницы
        transfer_amount = disparity / Decimal("2")
        transfer_amount = min(transfer_amount, self.config.rebalance_min_transfer_usdt)

        return {
            "from_exchange": from_ex,
            "to_exchange": to_ex,
            "asset": "USDT",
            "amount": transfer_amount,
            "reason": f"Дисбаланс: {disparity} USDT разницы",
        }
