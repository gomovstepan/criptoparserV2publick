"""Расчёт net spread с учётом комиссий, slippage (VWAP) и withdrawal fee."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class VWAPResult:
    """Результат расчёта VWAP для стороны стакана."""

    vwap: Decimal
    best_price: Decimal
    slippage_pct: Decimal
    base_acquired: Decimal
    quote_spent: Decimal
    levels_used: int
    liquidity_exhausted: bool


@dataclass
class NetSpreadResult:
    """Полный результат расчёта арбитражного спреда с издержками."""

    gross_spread: Decimal
    net_spread: Decimal
    slippage_buy: Decimal
    slippage_sell: Decimal
    fee_total: Decimal
    withdrawal_pct: Decimal
    is_profitable: bool
    liquidity_exhausted: bool


class NetSpreadCalculator:
    """Калькулятор реалистичного net spread на основе стакана."""

    def __init__(self, fee_config, withdrawal_fee_usdt=Decimal("0")):
        """
        fee_config: dict {exchange_name: {"taker": Decimal, "maker": Decimal, "token_discount": Decimal}}
        withdrawal_fee_usdt: фиксированный fee перевода в USDT (0 для pre-funded)
        """
        self.fee_config = fee_config
        self.withdrawal_fee_usdt = Decimal(str(withdrawal_fee_usdt))

    def calculate_vwap(
        self, orderbook_levels: list, target_quote_value: Decimal
    ) -> VWAPResult:
        """
        Расчёт VWAP для покупки/продажи на target_quote_value USDT.

        orderbook_levels: список [{"price": Decimal, "volume": Decimal}, ...]
                          отсортирован по возрастанию цены для ASK,
                          по убыванию для BID.
        target_quote_value: сколько USDT хотим потратить/получить.
        """
        target_quote_value = Decimal(str(target_quote_value))
        total_quote = Decimal("0")
        total_base = Decimal("0")
        levels_used = 0

        if not orderbook_levels:
            return VWAPResult(
                vwap=Decimal("0"),
                best_price=Decimal("0"),
                slippage_pct=Decimal("0"),
                base_acquired=Decimal("0"),
                quote_spent=Decimal("0"),
                levels_used=0,
                liquidity_exhausted=True,
            )

        best_price = orderbook_levels[0]["price"]

        for level in orderbook_levels:
            price = level["price"]
            volume = level["volume"]

            if total_quote >= target_quote_value:
                break

            quote_available = price * volume
            quote_to_fill = min(quote_available, target_quote_value - total_quote)
            base_filled = quote_to_fill / price

            total_base += base_filled
            total_quote += quote_to_fill
            levels_used += 1

        if total_base == 0:
            return VWAPResult(
                vwap=Decimal("0"),
                best_price=best_price,
                slippage_pct=Decimal("0"),
                base_acquired=Decimal("0"),
                quote_spent=Decimal("0"),
                levels_used=0,
                liquidity_exhausted=True,
            )

        vwap = total_quote / total_base
        slippage_pct = (vwap - best_price) / best_price * Decimal("100")
        if slippage_pct < 0:
            # Для BID-стороны vwap может быть меньше best_price
            slippage_pct = (best_price - vwap) / best_price * Decimal("100")

        return VWAPResult(
            vwap=vwap,
            best_price=best_price,
            slippage_pct=slippage_pct.quantize(Decimal("0.0001")),
            base_acquired=total_base,
            quote_spent=total_quote,
            levels_used=levels_used,
            liquidity_exhausted=total_quote < target_quote_value,
        )

    def calculate_net_spread(
        self,
        gross_spread_pct: Decimal,
        buy_exchange: str,
        sell_exchange: str,
        orderbook_buy_asks: list,
        orderbook_sell_bids: list,
        target_quote_value: Decimal,
    ) -> NetSpreadResult:
        """
        Расчёт net spread с учётом всех издержек.

        orderbook_buy_asks: уровни ASK биржи покупки (отсортированы по возрастанию цены)
        orderbook_sell_bids: уровни BID биржи продажи (отсортированы по убыванию цены)
        """
        gross_spread_pct = Decimal(str(gross_spread_pct))
        target_quote_value = Decimal(str(target_quote_value))

        # 1. VWAP для покупки на бирже A (asks)
        vwap_buy = self.calculate_vwap(orderbook_buy_asks, target_quote_value)
        slippage_buy = vwap_buy.slippage_pct

        # 2. VWAP для продажи на бирже B (bids)
        vwap_sell = self.calculate_vwap(orderbook_sell_bids, target_quote_value)
        slippage_sell = vwap_sell.slippage_pct

        # 3. Комиссии
        fee_buy = self._effective_fee(buy_exchange)
        fee_sell = self._effective_fee(sell_exchange)
        fee_total = (fee_buy + fee_sell) * Decimal("100")

        # 4. Withdrawal fee как % от объёма
        trade_value = target_quote_value
        withdrawal_pct = Decimal("0")
        if trade_value > 0:
            withdrawal_pct = (self.withdrawal_fee_usdt / trade_value) * Decimal("100")

        # 5. Net spread
        net_spread = (
            gross_spread_pct
            - fee_total
            - slippage_buy
            - slippage_sell
            - withdrawal_pct
        )

        liquidity_exhausted = vwap_buy.liquidity_exhausted or vwap_sell.liquidity_exhausted

        return NetSpreadResult(
            gross_spread=gross_spread_pct.quantize(Decimal("0.0001")),
            net_spread=net_spread.quantize(Decimal("0.0001")),
            slippage_buy=slippage_buy,
            slippage_sell=slippage_sell,
            fee_total=fee_total.quantize(Decimal("0.0001")),
            withdrawal_pct=withdrawal_pct.quantize(Decimal("0.0001")),
            is_profitable=net_spread > 0,
            liquidity_exhausted=liquidity_exhausted,
        )

    def reload_fees(self, fee_config):
        """Обновляет конфигурацию комиссий на лету."""
        self.fee_config = fee_config

    def _effective_fee(self, exchange_name: str) -> Decimal:
        """Возвращает эффективный taker fee с учётом скидки токеном."""
        cfg = self.fee_config.get(exchange_name.lower(), {})
        taker = Decimal(str(cfg.get("taker", "0.10"))) / Decimal("100")
        token_discount = Decimal(str(cfg.get("token_discount", "0")))
        if token_discount > 0:
            taker = taker * (Decimal("1") - token_discount)
        return taker
