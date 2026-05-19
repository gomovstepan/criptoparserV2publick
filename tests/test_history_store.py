"""
Тесты backend/history_store.py — SQLite-хранилище.
"""
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from backend.history_store import ArbitrageHistoryStore


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "history.db"


class TestInsertAndList:
    def test_insert_and_list(self, temp_db):
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        store.insert_record("BTCUSDT", "binance", "bybit", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:10+00:00", Decimal("1.5"))
        records = store.list_records()
        assert len(records) == 1
        assert records[0].coin == "BTCUSDT"
        assert records[0].max_spread == Decimal("1.5")
        assert records[0].duration_seconds == 10

    def test_retention(self, temp_db):
        store = ArbitrageHistoryStore(str(temp_db), max_records=2)
        for i in range(5):
            store.insert_record("BTCUSDT", "a", "b", f"2024-01-01T00:00:0{i}+00:00", "2024-01-01T00:00:01+00:00", Decimal("1.0"))
        records = store.list_records()
        assert len(records) == 2

    def test_clear(self, temp_db):
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        store.insert_record("BTCUSDT", "a", "b", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:01+00:00", Decimal("1.0"))
        store.clear()
        assert store.list_records() == []

    def test_string_max_spread_accepted(self, temp_db):
        """
        MINOR: insert_record аннотирован как Decimal, но принимает str.
        Работает, но типизация нарушена.
        """
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        store.insert_record("BTCUSDT", "a", "b", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:01+00:00", "1.5")
        records = store.list_records()
        assert records[0].max_spread == Decimal("1.5")

    def test_duplicate_records_ignored(self, temp_db):
        """
        UNIQUE constraint + IntegrityError предотвращают дубли при повторной обработке.
        """
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        for _ in range(3):
            store.insert_record("BTCUSDT", "a", "b", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:01+00:00", Decimal("1.0"))
        assert len(store.list_records()) == 1

    def test_db_created_with_wal_mode_enabled(self, temp_db):
        """WAL mode включён для поддержки многопроцессного доступа."""
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        with sqlite3.connect(str(temp_db)) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert journal_mode.lower() == "wal"

    def test_unique_constraint_prevents_duplicates(self, temp_db):
        """UNIQUE constraint + IntegrityError защищают от дублирования событий."""
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        store.insert_record("BTCUSDT", "binance", "bybit", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:10+00:00", Decimal("1.5"))
        store.insert_record("BTCUSDT", "binance", "bybit", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:10+00:00", Decimal("1.5"))
        records = store.list_records()
        assert len(records) == 1  # дубль проигнорирован

    def test_initialize_deduplicates_existing_db(self, temp_db):
        """Если БД уже содержит дубли, _initialize() должна их удалить и не упасть."""
        # Создаём таблицу вручную БЕЗ unique constraint и unique index
        with sqlite3.connect(str(temp_db)) as conn:
            conn.execute(
                """
                CREATE TABLE arbitrage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    coin TEXT NOT NULL,
                    buy_exchange TEXT NOT NULL,
                    sell_exchange TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    max_spread TEXT NOT NULL
                )
                """
            )
            # Вставляем 3 дублярующиеся записи
            for _ in range(3):
                conn.execute(
                    """
                    INSERT INTO arbitrage_history
                    (coin, buy_exchange, sell_exchange, start_time, end_time, duration_seconds, max_spread)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("BTCUSDT", "binance", "bybit", "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:10+00:00", 10, "1.5"),
                )
            conn.commit()

        # Инициализация store — должна пройти без ошибок, дедуплицировав таблицу
        store = ArbitrageHistoryStore(str(temp_db), max_records=100)
        records = store.list_records()
        assert len(records) == 1
        assert records[0].max_spread == Decimal("1.5")
