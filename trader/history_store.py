"""SQLite хранилище истории торговых операций для аудита и PnL-анализа."""
import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

from trader.models import Position, TradeExecution


class TradeHistoryStore:
    """Хранит историю всех торговых операций для последующего анализа."""

    def __init__(self, db_path: str = "/data/trade_history.db"):
        self.db_path = db_path
        self.logger = logging.getLogger("trader.history")
        self._init_db()

    def _init_db(self):
        """Создает таблицы если они не существуют."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    event_key TEXT NOT NULL,
                    coin TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trade_type TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    planned_buy_price TEXT,
                    planned_sell_price TEXT,
                    planned_spread TEXT,
                    planned_net_spread TEXT,
                    confidence_at_entry TEXT,
                    target_value_usdt TEXT,
                    created_at TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    buy_trade_json TEXT,
                    sell_trade_json TEXT,
                    actual_pnl TEXT,
                    actual_pnl_percent TEXT,
                    duration_seconds INTEGER,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS balance_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    free TEXT NOT NULL,
                    total TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rebalance_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    from_exchange TEXT NOT NULL,
                    to_exchange TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    reason TEXT,
                    executed INTEGER DEFAULT 0
                )
            """)

    def save_position(self, position: Position):
        """Сохраняет или обновляет позицию."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO positions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                """, (
                    position.id, position.event_key, position.coin,
                    position.status.value if position.status else "",
                    position.trade_type.value if position.trade_type else "",
                    position.buy_exchange, position.sell_exchange,
                    str(position.planned_buy_price),
                    str(position.planned_sell_price),
                    str(position.planned_spread),
                    str(position.planned_net_spread),
                    str(position.confidence_at_entry),
                    str(position.target_value_usdt),
                    position.created_at.isoformat() if position.created_at else None,
                    position.opened_at.isoformat() if position.opened_at else None,
                    position.closed_at.isoformat() if position.closed_at else None,
                    json.dumps(self._trade_to_dict(position.buy_trade))
                        if position.buy_trade else None,
                    json.dumps(self._trade_to_dict(position.sell_trade))
                        if position.sell_trade else None,
                    str(position.actual_pnl),
                    str(position.actual_pnl_percent),
                    position.duration_seconds, position.notes,
                ))
        except Exception as e:
            self.logger.error("Ошибка сохранения позиции %s: %s", position.id, e)

    def _trade_to_dict(self, trade: Optional[TradeExecution]) -> Optional[dict]:
        """Преобразует TradeExecution в словарь для JSON-сериализации."""
        if not trade:
            return None
        return {
            "exchange": trade.exchange,
            "side": trade.side.value if trade.side else None,
            "symbol": trade.symbol,
            "amount": str(trade.amount),
            "price": str(trade.price),
            "filled_amount": str(trade.filled_amount),
            "filled_price": str(trade.filled_price),
            "fee": str(trade.fee),
            "fee_currency": trade.fee_currency,
            "order_id": trade.order_id,
            "status": trade.status,
            "timestamp": trade.timestamp.isoformat() if trade.timestamp else None,
            "is_simulated": trade.is_simulated,
        }

    def save_balance_snapshot(
        self,
        exchange: str,
        asset: str,
        free: Decimal,
        total: Decimal,
    ):
        """Сохраняет снапшот баланса."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO balance_snapshots
                        (timestamp, exchange, asset, free, total)
                        VALUES (?, ?, ?, ?, ?)""",
                    (datetime.now().isoformat(), exchange, asset, str(free), str(total)),
                )
        except Exception as e:
            self.logger.error("Ошибка сохранения снапшота баланса: %s", e)

    def get_positions(
        self,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Возвращает список позиций, отсортированных по времени создания."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if status:
                    rows = conn.execute(
                        """SELECT * FROM positions
                           WHERE status = ?
                           ORDER BY created_at DESC LIMIT ?""",
                        (status, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT * FROM positions
                           ORDER BY created_at DESC LIMIT ?""",
                        (limit,),
                    ).fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error("Ошибка получения позиций: %s", e)
            return []

    def get_pnl_summary(self, days: int = 7) -> Dict:
        """Возвращает сводку PnL за указанный период."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("""
                    SELECT
                        COUNT(*) as total_trades,
                        SUM(CASE WHEN CAST(actual_pnl AS REAL) > 0 THEN 1 ELSE 0 END)
                            as winning_trades,
                        SUM(CASE WHEN CAST(actual_pnl AS REAL) < 0 THEN 1 ELSE 0 END)
                            as losing_trades,
                        SUM(CAST(actual_pnl AS REAL)) as total_pnl,
                        AVG(CAST(actual_pnl_percent AS REAL)) as avg_pnl_percent,
                        AVG(duration_seconds) as avg_duration
                    FROM positions
                    WHERE status = 'CLOSED'
                    AND closed_at >= datetime('now', '-{} days')
                """.format(days)).fetchone()

                return {
                    "total_trades": row[0] or 0,
                    "winning_trades": row[1] or 0,
                    "losing_trades": row[2] or 0,
                    "total_pnl": Decimal(str(row[3] or 0)),
                    "avg_pnl_percent": Decimal(str(row[4] or 0)),
                    "avg_duration_seconds": row[5] or 0,
                    "win_rate": (row[1] / row[0] * 100) if row[0] else 0,
                }
        except Exception as e:
            self.logger.error("Ошибка расчета сводки PnL: %s", e)
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": Decimal("0"),
                "avg_pnl_percent": Decimal("0"),
                "avg_duration_seconds": 0,
                "win_rate": 0,
            }

    def get_exchange_pnl(self, days: int = 7) -> Dict[str, Decimal]:
        """Возвращает PnL по каждой бирже."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute("""
                    SELECT buy_exchange, SUM(CAST(actual_pnl AS REAL)) as pnl
                    FROM positions
                    WHERE status = 'CLOSED'
                    AND closed_at >= datetime('now', '-{} days')
                    GROUP BY buy_exchange
                """.format(days)).fetchall()
                return {row[0]: Decimal(str(row[1])) for row in rows if row[1]}
        except Exception as e:
            self.logger.error("Ошибка расчета PnL по биржам: %s", e)
            return {}

    def save_rebalance_operation(
        self,
        from_exchange: str,
        to_exchange: str,
        asset: str,
        amount: Decimal,
        reason: str = "",
    ):
        """Сохраняет планируемую операцию ребалансировки."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO rebalance_operations
                        (timestamp, from_exchange, to_exchange, asset, amount, reason)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        datetime.now().isoformat(), from_exchange,
                        to_exchange, asset, str(amount), reason,
                    ),
                )
        except Exception as e:
            self.logger.error("Ошибка сохранения операции ребаланса: %s", e)
