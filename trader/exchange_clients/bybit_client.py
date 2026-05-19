"""Bybit testnet клиент для demo trading."""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from trader.exchange_clients.base import BaseExchangeClient
from trader.models import Balance, TradeExecution, TradeSide


class BybitTestnetClient(BaseExchangeClient):
    """Клиент для Bybit testnet. Требует API ключи от testnet.bybit.com.

    Использует библиотеку pybit для взаимодействия с Bybit API.
    Работает в режиме testnet (демо-торговля с фиктивными средствами).

    Attributes:
        client: Экземпляр pybit HTTP клиента.
    """

    def __init__(self, credentials: dict):
        super().__init__("bybit", credentials)
        self.client = None
        self.logger = logging.getLogger("trader.bybit")

    async def connect(self) -> bool:
        """Подключается к Bybit testnet API.

        Returns:
            True если подключение успешно, иначе False.
        """
        try:
            from pybit.unified_trading import HTTP
            self.client = HTTP(
                testnet=True,
                api_key=self.credentials.get("api_key", ""),
                api_secret=self.credentials.get("api_secret", ""),
            )
            # Проверяем подключение получением информации об аккаунте
            info = self.client.get_account_info()
            self.logger.info("Bybit testnet connected: %s", info)
            return True
        except Exception as e:
            self.logger.error("Bybit testnet connection failed: %s", e)
            return False

    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива на Bybit testnet.

        Args:
            asset: Название актива (по умолчанию USDT).

        Returns:
            Объект Balance или None в случае ошибки.
        """
        try:
            result = self.client.get_wallet_balance(
                accountType="UNIFIED",
                coin=asset,
            )
            coin_data = result \
                .get("result", {}) \
                .get("list", [{}])[0] \
                .get("coin", [{}])[0]
            free = Decimal(str(coin_data.get("walletBalance", "0")))
            locked = Decimal(str(coin_data.get("locked", "0")))
            return Balance(
                exchange=self.exchange_name,
                asset=asset,
                free=free,
                locked=locked,
                total=free + locked,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            self.logger.error("Bybit get_balance failed for %s: %s", asset, e)
            return None

    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Размещает рыночный ордер на Bybit testnet.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            TradeExecution при успехе, None при ошибке.
        """
        try:
            bybit_side = "Buy" if side == TradeSide.BUY else "Sell"
            # Bybit использует category="spot" для спотовых ордеров
            result = self.client.place_order(
                category="spot",
                symbol=symbol.upper(),
                side=bybit_side,
                orderType="Market",
                qty=str(amount),
            )
            order_id = result.get("result", {}).get("orderId", "")

            # Получаем детали исполнения ордера
            order_info = self.client.get_order_history(
                category="spot",
                orderId=order_id,
            )
            order = order_info.get("result", {}).get("list", [{}])[0]

            filled_price = Decimal(str(order.get("avgPrice", "0")))
            filled_qty = Decimal(str(order.get("cumExecQty", "0")))
            fee = Decimal(str(order.get("cumExecFee", "0")))

            self.logger.info(
                "Bybit order filled: %s %s %s @ %s (fee=%s)",
                side.value, symbol, filled_qty, filled_price, fee
            )

            return TradeExecution(
                exchange=self.exchange_name,
                side=side,
                symbol=symbol.upper(),
                amount=amount,
                price=filled_price,
                filled_amount=filled_qty,
                filled_price=filled_price,
                fee=fee,
                fee_currency="USDT",
                order_id=order_id,
                status="FILLED",
                timestamp=datetime.now(timezone.utc),
                is_simulated=False,
            )
        except Exception as e:
            self.logger.error(
                "Bybit place_market_order failed: %s %s %s: %s",
                side.value, symbol, amount, e
            )
            return None

    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Получает текущую цену символа на Bybit.

        Args:
            symbol: Торговая пара.

        Returns:
            Текущая цена или None в случае ошибки.
        """
        try:
            result = self.client.get_tickers(
                category="spot",
                symbol=symbol.upper(),
            )
            price = result \
                .get("result", {}) \
                .get("list", [{}])[0] \
                .get("lastPrice", "0")
            return Decimal(str(price))
        except Exception as e:
            self.logger.error("Bybit get_symbol_price failed for %s: %s", symbol, e)
            return None

    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Получает минимальный размер ордера для символа.

        Args:
            symbol: Торговая пара.

        Returns:
            Минимальный размер ордера (по умолчанию 1).
        """
        try:
            result = self.client.get_instruments_info(
                category="spot",
                symbol=symbol.upper(),
            )
            min_qty = result \
                .get("result", {}) \
                .get("list", [{}])[0] \
                .get("lotSizeFilter", {}) \
                .get("minOrderQty", "1")
            return Decimal(str(min_qty))
        except Exception:
            self.logger.warning(
                "Failed to get min order size for %s, using default",
                symbol
            )
            return Decimal("1")  # default

    async def close(self):
        """Закрывает соединение с Bybit API."""
        self.client = None
        self.logger.info("Bybit client connection closed")
