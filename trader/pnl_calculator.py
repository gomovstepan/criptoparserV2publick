"""Калькулятор PnL — реальный и потенциальный."""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from trader.config import TraderConfig
from trader.models import Position, TradeSignal
from trader.history_store import TradeHistoryStore


class PnLCalculator:
    """Рассчитывает PnL и статистику торговых операций."""

    def __init__(self, config: TraderConfig, history_store: TradeHistoryStore):
        self.config = config
        self.history = history_store
        self.logger = logging.getLogger("trader.pnl")

    def calculate_potential_pnl(
        self,
        signal: TradeSignal,
        amount_usdt: Decimal,
    ) -> Dict:
        """Рассчитывает потенциальный PnL на основе сигнала (без исполнения).

        Используется для paper trading и предварительного анализа сделки
        перед входом в позицию.
        """
        # Покупка на бирже с меньшей ценой
        buy_cost = amount_usdt
        buy_fee = buy_cost * signal.fee_total / Decimal("100")

        # Продажа на бирже с большей ценой
        spread_multiplier = Decimal("1") + signal.spread_percent / Decimal("100")
        sell_revenue = amount_usdt * spread_multiplier
        sell_fee = sell_revenue * signal.fee_total / Decimal("100")

        # Чистый PnL после комиссий
        gross_pnl = sell_revenue - buy_cost
        total_fees = buy_fee + sell_fee
        net_pnl = gross_pnl - total_fees
        net_pnl_percent = (net_pnl / buy_cost * Decimal("100"))

        return {
            "investment": amount_usdt,
            "buy_cost": buy_cost,
            "sell_revenue": sell_revenue,
            "gross_pnl": gross_pnl,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "net_pnl_percent": net_pnl_percent.quantize(Decimal("0.0001")),
            "signal_net_spread": signal.net_spread,
            "is_profitable": net_pnl > 0,
        }

    def get_daily_report(self) -> Dict:
        """Ежедневный отчет по торговле."""
        try:
            summary = self.history.get_pnl_summary(days=1)
            by_exchange = self.history.get_exchange_pnl(days=1)

            return {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "summary": summary,
                "pnl_by_exchange": {k: str(v) for k, v in by_exchange.items()},
            }
        except Exception as e:
            self.logger.error("Ошибка формирования дневного отчета: %s", e)
            return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "error": str(e)}

    def get_weekly_report(self) -> Dict:
        """Недельный отчет по торговле."""
        try:
            summary = self.history.get_pnl_summary(days=7)
            by_exchange = self.history.get_exchange_pnl(days=7)

            return {
                "period": "7 days",
                "summary": summary,
                "pnl_by_exchange": {k: str(v) for k, v in by_exchange.items()},
            }
        except Exception as e:
            self.logger.error("Ошибка формирования недельного отчета: %s", e)
            return {"period": "7 days", "error": str(e)}

    def get_monthly_report(self) -> Dict:
        """Месячный отчет по торговле."""
        try:
            summary = self.history.get_pnl_summary(days=30)
            by_exchange = self.history.get_exchange_pnl(days=30)

            return {
                "period": "30 days",
                "summary": summary,
                "pnl_by_exchange": {k: str(v) for k, v in by_exchange.items()},
            }
        except Exception as e:
            self.logger.error("Ошибка формирования месячного отчета: %s", e)
            return {"period": "30 days", "error": str(e)}

    def format_report(self, report: Dict) -> str:
        """Форматирует отчет в читаемый текст для вывода в лог."""
        s = report.get("summary", {})
        lines = [
            f"=== Отчет по торговле ({report.get('date', report.get('period', ''))}) ===",
            f"Всего сделок: {s.get('total_trades', 0)}",
            f"Прибыльных: {s.get('winning_trades', 0)} | Убыточных: {s.get('losing_trades', 0)}",
            f"Win rate: {s.get('win_rate', 0):.1f}%",
            f"Общий PnL: {s.get('total_pnl', Decimal('0'))} USDT",
            f"Средний PnL: {s.get('avg_pnl_percent', Decimal('0')):.4f}%",
            f"Средняя длительность: {s.get('avg_duration_seconds', 0):.0f} сек",
            "--- PnL по биржам ---",
        ]
        for ex, pnl in report.get("pnl_by_exchange", {}).items():
            lines.append(f"  {ex}: {pnl} USDT")
        return "\n".join(lines)

    def compare_potential_vs_actual(self, position: Position) -> Dict:
        """Сравнивает потенциальный и фактический PnL позиции.

        Показывает slippage и эффективность исполнения.
        """
        # Потенциальный PnL на основе плановых значений
        if position.planned_net_spread and position.target_value_usdt:
            potential_pnl = (
                position.target_value_usdt *
                position.planned_net_spread /
                Decimal("100")
            )
        else:
            potential_pnl = Decimal("0")

        # Фактический PnL
        actual_pnl = position.actual_pnl

        # Slippage = разница между потенциальным и фактическим
        slippage = actual_pnl - potential_pnl

        return {
            "potential_pnl": potential_pnl,
            "actual_pnl": actual_pnl,
            "slippage": slippage,
            "slippage_percent": (
                (slippage / potential_pnl * Decimal("100"))
                if potential_pnl else Decimal("0")
            ),
            "efficiency": (
                (actual_pnl / potential_pnl * Decimal("100"))
                if potential_pnl else Decimal("0")
            ),
        }

    def estimate_trade_profitability(
        self,
        signal: TradeSignal,
    ) -> Dict:
        """Быстрая оценка прибыльности сигнала перед входом в сделку."""
        # Используем target_value из сигнала как оценочную сумму
        estimate = self.calculate_potential_pnl(
            signal, signal.target_value_usdt
        )

        # Дополнительные метрики
        confidence_factor = signal.confidence / Decimal("100")
        risk_adjusted_pnl = estimate["net_pnl"] * confidence_factor

        return {
            **estimate,
            "confidence_factor": confidence_factor,
            "risk_adjusted_pnl": risk_adjusted_pnl,
            "recommendation": self._get_recommendation(estimate, signal),
        }

    def _get_recommendation(
        self,
        estimate: Dict,
        signal: TradeSignal,
    ) -> str:
        """Формирует торговую рекомендацию на основе оценки."""
        if not estimate["is_profitable"]:
            return "SKIP: Сделка неприбыльная после комиссий"
        if signal.net_spread < self.config.min_net_spread:
            return "SKIP: Net spread ниже минимального порога"
        if signal.confidence < self.config.confidence_threshold:
            return "SKIP: Confidence ниже порога"
        if estimate["net_pnl_percent"] < Decimal("0.1"):
            return "HOLD: Прибыль слишком маленькая"
        return "EXECUTE: Сигнал подходит для входа"
