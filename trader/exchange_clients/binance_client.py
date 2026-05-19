"""Binance testnet клиент для demo trading."""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from trader.exchange_clients.base import BaseExchangeClient
from trader.models import Balance, TradeExecution, TradeSide


class BinanceTestnetClient(BaseExchangeClient):
    """Клиент для Binance spot testnet. Требует API ключи от testnet.binance.vision.

    Использует библиотеку python-binance для взаимодействия с Binance API.
    Работает в режиме testnet (демо-торговля с фиктивными средствами).

    Attributes:
        client: Экземпляр binance Client.
    """

    def __init__(self, credentials: dict):
        super().__init__("binance", credentials)
        self.client = None
        self.logger = logging.getLogger("trader.binance")

    async def connect(self) -> bool:
        """Подключается к Binance testnet API.

        Returns:
            True если подключение успешно, иначе False.
        """
        try:
            from binance.client import Client
            self.client = Client(
                api_key=self.credentials.get("api_key", ""),
                api_secret=self.credentials.get("api_secret", ""),
                testnet=True,
            )
            account = self.client.get_account()
            self.logger.info(
                "Binance testnet connected. Can trade: %s",
                account.get("canTrade")
            )
            return True
        except Exception as e:
            self.logger.error("Binance testnet connection failed: %s", e)
            return False

    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива на Binance testnet.

        Args:
            asset: Название актива (по умолчанию USDT).

        Returns:
            Объект Balance или None в случае ошибки.
        """
        try:
            account = self.client.get_account()
            for b in account.get("balances", []):
                if b["asset"] == asset.upper():
                    free = Decimal(str(b["free"]))
                    locked = Decimal(str(b["locked"]))
                    return Balance(
                        exchange=self.exchange_name,
                        asset=asset.upper(),
                        free=free,
                        locked=locked,
                        total=free + locked,
                        timestamp=datetime.now(timezone.utc),
                    )
            # Актив не найден — возвращаем нулевой баланс
            return Balance(
                exchange=self.exchange_name,
                asset=asset.upper(),
                free=Decimal("0"),
                locked=Decimal("0"),
                total=Decimal("0"),
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            self.logger.error("Binance get_balance failed for %s: %s", asset, e)
            return None

    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Размещает рыночный ордер на Binance testnet.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            TradeExecution при успехе, None при ошибке.
        """
        try:
            binance_side = "BUY" if side == TradeSide.BUY else "SELL"
            result = self.client.order_market_buy(
                symbol=symbol.upper(),
                quantity=str(amount),
            ) if side == TradeSide.BUY else self.client.order_market_sell(
                symbol=symbol.upper(),
                quantity=str(amount),
            )

            filled_price = Decimal(str(
                result.get("fills", [{}])[0].get("price", "0") if result.get("fills") else "0"
            ))
            filled_qty = Decimal(str(result.get("executedQty", "0")))
            fee = sum(
                Decimal(str(f["commission"]))
                for f in result.get("fills", [])
            )

            self.logger.info(
                "Binance order filled: %s %s %s @ %s (fee=%s)",
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
                order_id=str(result.get("orderId", "")),
                status="FILLED",
                timestamp=datetime.now(timezone.utc),
                is_simulated=False,
            )
        except Exception as e:
            self.logger.error(
                "Binance place_market_order failed: %s %s %s: %s",
                side.value, symbol, amount, e
            )
            return None

    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Получает текущую цену символа на Binance.

        Args:
            symbol: Торговая пара.

        Returns:
            Текущая цена или None в случае ошибки.
        """
        try:
            ticker = self.client.get_symbol_ticker(symbol=symbol.upper())
            return Decimal(str(ticker.get("price", "0")))
        except Exception as e:
            self.logger.error(
                "Binance get_symbol_price failed for %s: %s", symbol, e
            )
            return None

    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Получает минимальный размер ордера (в USDT эквиваленте).

        Args:
            symbol: Торговая пара.

        Returns:
            Минимальный размер ордера (по умолчанию 5 USDT).
        """
        try:
            info = self.client.get_symbol_info(symbol.upper())
            for f in info.get("filters", []):
                if f["filterType"] == "MIN_NOTIONAL":
                    return Decimal(str(f["minNotional"]))
                if f["filterType"] == "LOT_SIZE":
                    return Decimal(str(f["minQty"]))
            return Decimal("5")
        except Exception:
            self.logger.warning(
                "Failed to get min order size for %s, using default",
                symbol
            )
            return Decimal("5")

    async def close(self):
        """Закрывает соединение с Binance API."""
        self.client = None
        self.logger.info("Binance client connection closed")
