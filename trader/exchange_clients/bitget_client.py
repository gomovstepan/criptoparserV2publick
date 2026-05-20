"""Bitget demo trading клиент."""
import base64
import hmac
import hashlib
import json
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from trader.exchange_clients.base import BaseExchangeClient
from trader.models import Balance, TradeExecution, TradeSide


class BitgetDemoClient(BaseExchangeClient):
    """Клиент для Bitget demo trading.

    Demo API endpoint: https://api.bitget.com
    Используются demo API ключи из аккаунта Bitget.
    В настройках API нужно создать ключи и выбрать 'Demo Trading'.

    Реализован на чистом HTTP без внешних зависимостей (кроме стандартной
    библиотеки), используя официальный REST API Bitget.
    """

    BASE_URL = "https://api.bitget.com"

    def __init__(self, credentials: dict):
        super().__init__("bitget", credentials)
        self.logger = logging.getLogger("trader.bitget")

    def _generate_signature(
        self, timestamp: str, method: str, request_path: str, body: str = ""
    ) -> str:
        """Генерирует HMAC-SHA256 подпись для запроса к Bitget API.

        Args:
            timestamp: Текущая метка времени в миллисекундах.
            method: HTTP метод (GET/POST).
            request_path: Путь запроса (например, /api/v2/spot/account/assets).
            body: Тело запроса (для POST).

        Returns:
            Base64-encoded подпись.
        """
        message = timestamp + method.upper() + request_path + body
        mac = hmac.new(
            self.credentials.get("api_secret", "").encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _request(
        self, method: str, path: str, body: Optional[Dict] = None
    ) -> Dict:
        """Выполняет HTTP запрос к Bitget API.

        Args:
            method: HTTP метод (GET/POST).
            path: Путь запроса.
            body: Тело запроса для POST.

        Returns:
            Ответ API в виде словаря.

        Raises:
            urllib.error.HTTPError: При ошибке HTTP.
            json.JSONDecodeError: При ошибке парсинга ответа.
        """
        timestamp = str(int(time.time() * 1000))
        body_json = json.dumps(body) if body else ""
        signature = self._generate_signature(timestamp, method, path, body_json)

        headers = {
            "ACCESS-KEY": self.credentials.get("api_key", ""),
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.credentials.get("passphrase", ""),
            "Content-Type": "application/json",
        }

        url = f"{self.BASE_URL}{path}"
        data = body_json.encode("utf-8") if body_json else None
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method.upper()
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    async def connect(self) -> bool:
        """Подключается к Bitget demo API.

        Returns:
            True если подключение успешно, иначе False.
        """
        try:
            result = self._request("GET", "/api/v2/spot/account/info")
            self.logger.info("Bitget demo connected: %s", result)
            return True
        except Exception as e:
            self.logger.error("Bitget demo connection failed: %s", e)
            return False

    async def get_balance(self, asset: str = "USDT") -> Optional[Balance]:
        """Получает баланс актива на Bitget demo.

        Args:
            asset: Название актива (по умолчанию USDT).

        Returns:
            Объект Balance или None в случае ошибки.
        """
        try:
            result = self._request("GET", "/api/v2/spot/account/assets")
            for item in result.get("data", []):
                if item.get("coin") == asset.upper():
                    available = Decimal(str(item.get("available", "0")))
                    frozen = Decimal(str(item.get("frozen", "0")))
                    locked = Decimal(str(item.get("locked", "0")))
                    total = available + frozen + locked
                    return Balance(
                        exchange=self.exchange_name,
                        asset=asset.upper(),
                        free=available,
                        locked=frozen + locked,
                        total=total,
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
            self.logger.error("Bitget get_balance failed for %s: %s", asset, e)
            return None

    async def place_market_order(
        self, symbol: str, side: TradeSide, amount: Decimal
    ) -> Optional[TradeExecution]:
        """Размещает рыночный ордер на Bitget demo.

        Args:
            symbol: Торговая пара (например, BTCUSDT).
            side: Сторона сделки (BUY/SELL).
            amount: Количество базового актива.

        Returns:
            TradeExecution при успехе, None при ошибке.
        """
        try:
            bitget_side = "buy" if side == TradeSide.BUY else "sell"
            # Bitget spot market order
            body = {
                "symbol": symbol.upper(),
                "side": bitget_side,
                "orderType": "market",
                "size": str(amount),
            }
            result = self._request(
                "POST", "/api/v2/spot/trade/placeOrder", body
            )
            order_id = result.get("data", {}).get("orderId", "")

            self.logger.info(
                "Bitget order placed: %s %s %s, id=%s",
                side.value, symbol, amount, order_id
            )

            # Получаем детали ордера
            try:
                order_details = self._request(
                    "GET",
                    f"/api/v2/spot/trade/orderInfo?orderId={order_id}&symbol={symbol.upper()}"
                )
                order_data = order_details.get("data", [{}])[0]
                filled_price = Decimal(str(order_data.get("priceAvg", "0")))
                filled_qty = Decimal(str(order_data.get("baseVolume", "0")))
                fee = Decimal(str(order_data.get("fee", "0")))
                order_status = order_data.get("status", "PENDING")
            except Exception:
                filled_price = Decimal("0")
                filled_qty = Decimal("0")
                fee = Decimal("0")
                order_status = "PENDING"

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
                status=order_status.upper(),
                timestamp=datetime.now(timezone.utc),
                is_simulated=False,
            )
        except Exception as e:
            self.logger.error(
                "Bitget place_market_order failed: %s %s %s: %s",
                side.value, symbol, amount, e
            )
            return None

    async def get_symbol_price(self, symbol: str) -> Optional[Decimal]:
        """Получает текущую цену символа на Bitget.

        Args:
            symbol: Торговая пара.

        Returns:
            Текущая цена или None в случае ошибки.
        """
        try:
            result = self._request(
                "GET",
                f"/api/v2/spot/market/ticker?symbol={symbol.upper()}"
            )
            return Decimal(str(result.get("data", [{}])[0].get("lastPr", "0")))
        except Exception as e:
            self.logger.error(
                "Bitget get_symbol_price failed for %s: %s", symbol, e
            )
            return None

    async def get_min_order_size(self, symbol: str) -> Decimal:
        """Минимальный размер ордера для Bitget.

        Args:
            symbol: Торговая пара.

        Returns:
            Минимальный размер ордера (по умолчанию 1).
        """
        return Decimal("1")

    async def close(self):
        """Bitget client не требует явного закрытия соединения."""
        self.logger.info("Bitget client connection closed")
