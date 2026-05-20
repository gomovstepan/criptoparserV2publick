"""Абстрактный базовый класс для всех биржевых клиентов."""
import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Dict, List, Optional

from trader.models import Balance, TradeExecution, TradeSide


class BaseExchangeClient(ABC):
    """Абстрактный базовый класс для биржевых клиентов.

    Все клиенты (demo, paper, real) должны наследовать этот класс
    и реализовать абстрактные методы.
    """

    def __init__(self, exchange_name: str, credentials: dict):
        self.exchange_name = exchange_name
        self.credentials = credentials
        self.logger = logging.getLogger(f"trader.{exchange_name}")

    @abstractmethod
    async def connect(self) -> bool:
        """Подключение к бирже. Возвращает True если успешно."""
        pass

    @abstractmethod
    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива.

        Args:
            asset: Название актива (по умолчанию USDT).

        Returns:
            Объект Balance или None в случае ошибки.
        """
        pass

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Размещает рыночный ордер.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            Объект TradeExecution или None в случае ошибки.
        """
        pass

    @abstractmethod
    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Получает текущую цену символа.

        Args:
            symbol: Торговая пара.

        Returns:
            Текущая цена или None в случае ошибки.
        """
        pass

    @abstractmethod
    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Минимальный размер ордера для символа.

        Args:
            symbol: Торговая пара.

        Returns:
            Минимальный размер ордера.
        """
        pass

    @abstractmethod
    async def close(self):
        """Закрывает соединение с биржей и освобождает ресурсы."""
        pass

    def normalize_symbol(self, symbol: str) -> str:
        """Нормализует символ для биржи (верхний регистр).

        Args:
            symbol: Торговая пара в любом регистре.

        Returns:
            Символ в верхнем регистре.
        """
        return symbol.upper()
