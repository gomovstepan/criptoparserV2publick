"""Paper trading клиент — имитирует торговлю внутри кода."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from trader.exchange_clients.base import BaseExchangeClient
from trader.models import Balance, TradeExecution, TradeSide


class PaperExchangeClient(BaseExchangeClient):
    """Клиент для paper trading — имитирует ордера и отслеживает баланс в памяти.

    Не требует API ключей. Используется для бирж без demo/testnet
    (GateIO, CoinEx, BingX и т.д.), а также как запасной вариант
    когда реальные API недоступны.

    Attributes:
        _balances: Разделяемое хранилище балансов (exchange -> asset -> amount).
        _lock: Асинхронный лок для потокобезопасного доступа к балансам.
    """

    # Разделяемое хранилище балансов (exchange -> asset -> amount)
    _balances: Dict[str, Dict[str, Decimal]] = {}
    _lock = asyncio.Lock()

    def __init__(self, exchange_name: str, initial_usdt: Decimal = Decimal("10000")):
        super().__init__(exchange_name, {})
        self.initial_usdt = initial_usdt
        self.logger = logging.getLogger(f"trader.paper.{exchange_name}")
        self._symbol_prices: Dict[str, Decimal] = {}
        self._initialize_balance()

    def _initialize_balance(self):
        """Инициализирует начальный баланс USDT для биржи."""
        if self.exchange_name not in PaperExchangeClient._balances:
            PaperExchangeClient._balances[self.exchange_name] = {
                "USDT": self.initial_usdt
            }
            self.logger.info(
                "Initialized paper balance for %s: %s USDT",
                self.exchange_name, self.initial_usdt
            )

    async def connect(self) -> bool:
        """Paper client всегда подключен."""
        self.logger.info("Paper client connected for %s", self.exchange_name)
        return True

    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива из памяти."""
        async with PaperExchangeClient._lock:
            amount = PaperExchangeClient._balances \
                .get(self.exchange_name, {}) \
                .get(asset, Decimal("0"))
        return Balance(
            exchange=self.exchange_name,
            asset=asset,
            free=amount,
            locked=Decimal("0"),
            total=amount,
            timestamp=datetime.now(timezone.utc),
        )

    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Имитирует рыночный ордер со slippage 0.1%% и комиссией 0.1%%.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            TradeExecution при успехе, None при недостатке средств или ошибке.
        """
        # Получаем текущую цену
        price = await self.get_symbol_price(symbol)
        if not price or price <= 0:
            self.logger.error("No price available for %s", symbol)
            return None

        # Имитируем slippage 0.1%
        slippage = Decimal("0.001")

        if side == TradeSide.BUY:
            filled_price = price * (Decimal("1") + slippage)
            quote_amount = amount * filled_price
            fee = quote_amount * Decimal("0.001")  # 0.1% fee

            # Проверяем баланс USDT
            balance = await self.get_balance("USDT")
            if balance.free < quote_amount + fee:
                self.logger.error(
                    "Insufficient USDT balance on %s: %s < %s (amount=%s, fee=%s)",
                    self.exchange_name, balance.free, quote_amount + fee,
                    quote_amount, fee
                )
                return None

            async with PaperExchangeClient._lock:
                ex_bal = PaperExchangeClient._balances[self.exchange_name]
                ex_bal["USDT"] = ex_bal.get("USDT", Decimal("0")) - quote_amount - fee
                base_asset = self._get_base_asset(symbol)
                ex_bal[base_asset] = ex_bal.get(base_asset, Decimal("0")) + amount

        else:  # SELL
            filled_price = price * (Decimal("1") - slippage)
            quote_amount = amount * filled_price
            fee = quote_amount * Decimal("0.001")  # 0.1% fee

            # Проверяем баланс base asset
            base_asset = self._get_base_asset(symbol)
            balance = await self.get_balance(base_asset)
            if balance.free < amount:
                self.logger.error(
                    "Insufficient %s balance on %s: %s < %s",
                    base_asset, self.exchange_name, balance.free, amount
                )
                return None

            async with PaperExchangeClient._lock:
                ex_bal = PaperExchangeClient._balances[self.exchange_name]
                ex_bal[base_asset] = ex_bal.get(base_asset, Decimal("0")) - amount
                ex_bal["USDT"] = ex_bal.get("USDT", Decimal("0")) + quote_amount - fee

        self.logger.info(
            "Paper order executed on %s: %s %s %s @ %s (fee=%s USDT)",
            self.exchange_name, side.value, symbol, amount, filled_price, fee
        )

        return TradeExecution(
            exchange=self.exchange_name,
            side=side,
            symbol=symbol.upper(),
            amount=amount,
            price=price,
            filled_amount=amount,
            filled_price=filled_price,
            fee=fee,
            fee_currency="USDT",
            order_id=f"paper_{uuid.uuid4().hex[:8]}",
            status="FILLED",
            timestamp=datetime.now(timezone.utc),
            is_simulated=True,
        )

    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Возвращает установленную цену символа или заглушку.

        В реальном сценарии цена устанавливается из SignalRouter
        на основе данных из Redis/orderbook.

        Args:
            symbol: Торговая пара.

        Returns:
            Цена символа или Decimal("100") как заглушка.
        """
        price = self._symbol_prices.get(symbol.upper())
        if price:
            return price
        # TODO: интеграция с Redis для реальных цен
        self.logger.debug("No price set for %s, returning default", symbol.upper())
        return Decimal("100")

    def set_symbol_price(self, symbol: str, price: Decimal):
        """Устанавливает цену символа (вызывается из SignalRouter).

        Args:
            symbol: Торговая пара.
            price: Цена символа.
        """
        self._symbol_prices[symbol.upper()] = price
        self.logger.debug("Price set for %s: %s", symbol.upper(), price)

    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Минимальный размер ордера для paper trading."""
        return Decimal("5")  # минимум 5 USDT

    async def close(self):
        """Paper client не требует закрытия соединения."""
        self.logger.info("Paper client closed for %s", self.exchange_name)

    def _get_base_asset(self, symbol: str) -> str:
        """Извлекает базовый актив из символа.

        Примеры:
            BTCUSDT -> BTC
            ETHUSDC -> ETH
            ETHBTC  -> ETH

        Args:
            symbol: Торговая пара.

        Returns:
            Название базового актива.
        """
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if symbol.upper().endswith(quote):
                return symbol.upper()[:-len(quote)]
        return symbol.upper()

    async def get_all_balances(self) -> Dict[str, Decimal]:
        """Возвращает все балансы биржи.

        Returns:
            Словарь {asset: amount}.
        """
        async with PaperExchangeClient._lock:
            return dict(PaperExchangeClient._balances.get(self.exchange_name, {}))
