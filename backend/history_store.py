import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass
class ArbitrageHistoryRecord:
    id: int
    coin: str
    buy_exchange: str
    sell_exchange: str
    start_time: str
    end_time: str
    duration_seconds: int
    max_spread: Decimal
    net_spread: Decimal = Decimal("0")
    fee_total: Decimal = Decimal("0")
    slippage_buy: Decimal = Decimal("0")
    slippage_sell: Decimal = Decimal("0")
    confidence: Decimal = Decimal("0")
    data_age_ms: int = 0


class ArbitrageHistoryStore:
    def __init__(self, db_path: str, max_records: int):
        self.db_path = Path(db_path)
        self.max_records = max_records
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS arbitrage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    max_spread TEXT NOT NULL,
                    net_spread TEXT DEFAULT '0',
                    fee_total TEXT DEFAULT '0',
                    slippage_buy TEXT DEFAULT '0',
                    slippage_sell TEXT DEFAULT '0',
                    confidence TEXT DEFAULT '0',
                    data_age_ms INTEGER DEFAULT 0,
                    UNIQUE (coin, buy_exchange, sell_exchange, start_time)
                )
                """
            )
            # Дедупликация перед созданием unique index — старые БД могут содержать дубли
            self._deduplicate_table(connection)
            try:
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_arbitrage_unique
                    ON arbitrage_history (coin, buy_exchange, sell_exchange, start_time)
                    """
                )
            except sqlite3.IntegrityError:
                # Если дубли всё ещё остались — повторная дедупликация и retry
                self._deduplicate_table(connection)
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_arbitrage_unique
                    ON arbitrage_history (coin, buy_exchange, sell_exchange, start_time)
                    """
                )
            # Миграция: добавляем колонки, если таблица уже существовала без них
            self._migrate_add_column(connection, "net_spread", "TEXT DEFAULT '0'")
            self._migrate_add_column(connection, "fee_total", "TEXT DEFAULT '0'")
            self._migrate_add_column(connection, "slippage_buy", "TEXT DEFAULT '0'")
            self._migrate_add_column(connection, "slippage_sell", "TEXT DEFAULT '0'")
            self._migrate_add_column(connection, "confidence", "TEXT DEFAULT '0'")
            self._migrate_add_column(connection, "data_age_ms", "INTEGER DEFAULT 0")
            connection.commit()

    def _deduplicate_table(self, connection):
        """Удаляет дублирующиеся записи, оставляя ту, у которой максимальный id."""
        connection.execute(
            """
            DELETE FROM arbitrage_history
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM arbitrage_history
                GROUP BY coin, buy_exchange, sell_exchange, start_time
            )
            """
        )

    def _migrate_add_column(self, connection, column_name, column_type):
        try:
            connection.execute(
                f"ALTER TABLE arbitrage_history ADD COLUMN {column_name} {column_type}"
            )
        except sqlite3.OperationalError:
            pass  # Колонка уже существует

    def insert_record(
        self,
        coin: str,
        buy_exchange: str,
        sell_exchange: str,
        start_time: str,
        end_time: str,
        max_spread: Decimal,
        net_spread: Decimal = Decimal("0"),
        fee_total: Decimal = Decimal("0"),
        slippage_buy: Decimal = Decimal("0"),
        slippage_sell: Decimal = Decimal("0"),
        confidence: Decimal = Decimal("0"),
        data_age_ms: int = 0,
    ):
        duration_seconds = self._duration_seconds(start_time, end_time)

        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO arbitrage_history
                    (coin, buy_exchange, sell_exchange, start_time, end_time, duration_seconds,
                     max_spread, net_spread, fee_total, slippage_buy, slippage_sell, confidence, data_age_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        coin,
                        buy_exchange,
                        sell_exchange,
                        start_time,
                        end_time,
                        duration_seconds,
                        str(max_spread),
                        str(net_spread),
                        str(fee_total),
                        str(slippage_buy),
                        str(slippage_sell),
                        str(confidence),
                        data_age_ms,
                    ),
                )
                self._enforce_retention(connection)
                connection.commit()
            except sqlite3.IntegrityError:
                # Игнорируем дублирующиеся события (идемпотентность).
                pass

    def list_records(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, coin, buy_exchange, sell_exchange, start_time, end_time, duration_seconds,
                       max_spread, net_spread, fee_total, slippage_buy, slippage_sell, confidence, data_age_ms
                FROM arbitrage_history
                ORDER BY id DESC
                """
            ).fetchall()

        records = []
        for row in rows:
            records.append(
                ArbitrageHistoryRecord(
                    id=row["id"],
                    coin=row["coin"],
                    buy_exchange=row["buy_exchange"],
                    sell_exchange=row["sell_exchange"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    duration_seconds=row["duration_seconds"],
                    max_spread=Decimal(row["max_spread"]),
                    net_spread=Decimal(row["net_spread"] or "0"),
                    fee_total=Decimal(row["fee_total"] or "0"),
                    slippage_buy=Decimal(row["slippage_buy"] or "0"),
                    slippage_sell=Decimal(row["slippage_sell"] or "0"),
                    confidence=Decimal(row["confidence"] or "0"),
                    data_age_ms=row["data_age_ms"] or 0,
                )
            )
        return records

    def clear(self):
        with self._connect() as connection:
            connection.execute("DELETE FROM arbitrage_history")
            connection.commit()

    def _duration_seconds(self, start_time: str, end_time: str):
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)
        return max(0, int((end_dt - start_dt).total_seconds()))

    def _enforce_retention(self, connection):
        if self.max_records <= 0:
            return

        connection.execute(
            """
            DELETE FROM arbitrage_history
            WHERE id NOT IN (
                SELECT id
                FROM arbitrage_history
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (self.max_records,),
        )
