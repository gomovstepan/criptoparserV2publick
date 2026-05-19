"""KuCoin paper trade клиент для demo trading."""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from trader.exchange_clients.base import BaseExchangeClient
from trader.models import Balance, TradeExecution, TradeSide


class KuCoinPaperClient(BaseExchangeClient):
    """Клиент для KuCoin paper trade API.

    Paper trade аккаунт создается на https://www.kucoin.com/account/api
    — нужно выбрать 'Paper Trading' при создании API ключей.

    Использует библиотеку kucoin-python для взаимодействия с API.
    """

    def __init__(self, credentials: dict):
        super().__init__("kucoin", credentials)
        self._trade_client = None
        self._user_client = None
        self.logger = logging.getLogger("trader.kucoin")

    async def connect(self) -> bool:
        """Подключается к KuCoin paper trade API.

        Returns:
            True если подключение успешно, иначе False.
        """
        try:
            from kucoin.client import Trade, User
            # Paper trade использует sandbox режим с paper API ключами
            self._trade_client = Trade(
                key=self.credentials.get("api_key", ""),
                secret=self.credentials.get("api_secret", ""),
                passphrase=self.credentials.get("passphrase", ""),
                is_sandbox=True,  # paper trade режим
            )
            self._user_client = User(
                key=self.credentials.get("api_key", ""),
                secret=self.credentials.get("api_secret", ""),
                passphrase=self.credentials.get("passphrase", ""),
                is_sandbox=True,
            )
            # Проверяем подключение получением списка аккаунтов
            balances = self._user_client.get_account_list()
            self.logger.info("KuCoin paper connected. Accounts: %s", len(balances))
            return True
        except Exception as e:
            self.logger.error("KuCoin paper connection failed: %s", e)
            return False

    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива на KuCoin paper.

        Args:
            asset: Название актива (по умолчанию USDT).

        Returns:
            Объект Balance или None в случае ошибки.
        """
        try:
            accounts = self._user_client.get_account_list(currency=asset)
            free = Decimal("0")
            locked = Decimal("0")
            for acc in accounts:
                if acc.get("currency") == asset.upper():
                    free += Decimal(str(acc.get("available", "0")))
                    locked += Decimal(str(acc.get("holds", "0")))
            return Balance(
                exchange=self.exchange_name,
                asset=asset.upper(),
                free=free,
                locked=locked,
                total=free + locked,
                timestamp=datetime.now(timezone.utc),
            )
        except Exception as e:
            self.logger.error("KuCoin get_balance failed for %s: %s", asset, e)
            return None

    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Размещает рыночный ордер на KuCoin paper.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            TradeExecution при успехе, None при ошибке.
        """
        try:
            kucoin_side = "buy" if side == TradeSide.BUY else "sell"
            # KuCoin формат символа: BTC-USDT
            kucoin_symbol = self._normalize_kucoin_symbol(symbol)

            result = self._trade_client.create_market_order(
                symbol=kucoin_symbol,
                side=kucoin_side,
                size=str(amount),
            )
            order_id = result.get("orderId", "")

            self.logger.info(
                "KuCoin order placed: %s %s %s, orderId=%s",
                side.value, kucoin_symbol, amount, order_id
            )

            # Даем время на исполнение ордера
            await asyncio.sleep(0.5)

            # Получаем детали исполненного ордера
            order = self._trade_client.get_order_details(orderId=order_id)

            deal_size = Decimal(str(order.get("dealSize", "0")))
            deal_funds = Decimal(str(order.get("dealFunds", "0")))
            filled_price = (
                deal_funds / deal_size if deal_size > 0 else Decimal("0")
            )
            filled_qty = deal_size
            fee = Decimal(str(order.get("fee", "0")))

            self.logger.info(
                "KuCoin order filled: %s %s %s @ %s (fee=%s)",
                side.value, kucoin_symbol, filled_qty, filled_price, fee
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
                "KuCoin place_market_order failed: %s %s %s: %s",
                side.value, symbol, amount, e
            )
            return None

    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Получает текущую цену символа на KuCoin.

        Args:
            symbol: Торговая пара (в формате BTCUSDT).

        Returns:
            Текущая цена или None в случае ошибки.
        """
        try:
            from kucoin.client import Market
            kucoin_symbol = self._normalize_kucoin_symbol(symbol)
            market = Market()
            ticker = market.get_ticker(symbol=kucoin_symbol)
            return Decimal(str(ticker.get("price", "0")))
        except Exception as e:
            self.logger.error(
                "KuCoin get_symbol_price failed for %s: %s", symbol, e
            )
            return None

    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Минимальный размер ордера для KuCoin.

        Args:
            symbol: Торговая пара.

        Returns:
            Минимальный размер ордера.
        """
        return Decimal("0.1")

    async def close(self):
        """Закрывает соединение с KuCoin API."""
        self._trade_client = None
        self._user_client = None
        self.logger.info("KuCoin client connection closed")

    def _normalize_kucoin_symbol(self, symbol: str) -> str:
        """Конвертирует символ в формат KuCoin.

        Примеры:
            BTCUSDT -> BTC-USDT
            ETHUSDC -> ETH-USDC

        Args:
            symbol: Торговая пара в стандартном формате.

        Returns:
            Торговая пара в формате KuCoin.
        """
        s = symbol.upper()
        for quote in ["USDT", "USDC", "BTC", "ETH"]:
            if s.endswith(quote):
                base = s[:-len(quote)]
                return f"{base}-{quote}"
        return s  # fallback
