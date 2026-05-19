"""
Тесты exchanges/http_polling.py и парсинга ответов бирж.
"""
import asyncio
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from exchanges.http_polling import (
    HttpPollingOrderBookConnection,
    build_url,
    chunk_symbols,
    http_get_json,
    http_post_json,
    normalize_levels,
)


class TestNormalizeLevels:
    def test_tuple_format(self):
        raw = [["100.5", "10"], ["101.0", "5"]]
        result = normalize_levels(raw)
        assert result == [["100.5", "10"], ["101", "5"]]

    def test_dict_price_amount(self):
        raw = [{"price": "100.5", "amount": "10"}, {"price": "101.0", "amount": "5"}]
        result = normalize_levels(raw)
        assert result == [["100.5", "10"], ["101", "5"]]

    def test_dict_p_q(self):
        raw = [{"p": "100.5", "q": "10"}]
        result = normalize_levels(raw)
        assert result == [["100.5", "10"]]

    def test_dict_size(self):
        raw = [{"price": "100.5", "size": "10"}]
        result = normalize_levels(raw)
        assert result == [["100.5", "10"]]

    def test_decimal_input_preserved(self):
        """Decimal преобразуется в строку без потери точности, trailing zeros убираются."""
        raw = [{"price": Decimal("0.0000001"), "amount": Decimal("100.0")}]
        result = normalize_levels(raw)
        # str(Decimal) может давать экспоненциальную нотацию, но Decimal восстанавливает точность
        assert Decimal(result[0][0]) == Decimal("1E-7")
        assert Decimal(result[0][1]) == Decimal("100")

    def test_zero_amount_respected(self):
        """
        volume=0 (удаление уровня) должен интерпретироваться как 0,
        а не как falsy для fallback на другой ключ.
        """
        raw = [{"price": "100.5", "amount": 0, "size": "10"}]
        result = normalize_levels(raw)
        assert result == [["100.5", "0"]]

    def test_non_list_input(self):
        assert normalize_levels(None) == []
        assert normalize_levels("bad") == []

    def test_missing_keys_skipped(self):
        raw = [{"price": "100.5"}, {"amount": "10"}]
        assert normalize_levels(raw) == []

    def test_trailing_zeros_stripped(self):
        """Trailing zeros в цене должны удаляться (0.24810000 → 0.2481)."""
        raw = [["0.24810000", "10"], ["84.120000", "5"], ["9.08000000", "1"]]
        result = normalize_levels(raw)
        assert result == [["0.2481", "10"], ["84.12", "5"], ["9.08", "1"]]


class TestChunkSymbols:
    def test_basic(self):
        assert chunk_symbols(["a", "b", "c", "d"], 2) == [["a", "b"], ["c", "d"]]

    def test_remainder(self):
        assert chunk_symbols(["a", "b", "c"], 2) == [["a", "b"], ["c"]]

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            chunk_symbols(["a"], 0)


class TestBuildUrl:
    def test_basic(self):
        url = build_url("https://api.example.com/book", {"symbol": "BTCUSDT", "limit": 10})
        assert url.startswith("https://api.example.com/book?")
        assert "symbol=BTCUSDT" in url
        assert "limit=10" in url


class TestParallelPolling:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Проверяем, что Semaphore ограничивает число параллельных fetch-запросов."""
        from unittest.mock import AsyncMock

        store = AsyncMock()
        settings = {
            "exchange_name": "test",
            "poll_interval_seconds": 1,
            "reconnect_delay_seconds": 1,
        }
        semaphore = asyncio.Semaphore(2)

        class DummyConnection(HttpPollingOrderBookConnection):
            fetch_count = 0
            max_concurrent = 0
            current = 0

            async def fetch_symbol_orderbook(self, symbol):
                DummyConnection.current += 1
                DummyConnection.max_concurrent = max(DummyConnection.max_concurrent, DummyConnection.current)
                await asyncio.sleep(0.05)
                DummyConnection.current -= 1
                DummyConnection.fetch_count += 1
                return [["100", "1"]], [["99", "1"]]

        conn = DummyConnection(settings, store, 1, ["A", "B", "C", "D"], semaphore)
        await conn.poll_once()
        assert DummyConnection.fetch_count == 4
        assert DummyConnection.max_concurrent <= 2


class TestHttpGetJson:
    @pytest.mark.asyncio
    @patch("exchanges.http_polling.urllib.request.urlopen")
    @patch("exchanges.http_polling.urllib.request.Request")
    async def test_success(self, mock_request, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"key": "value"}'
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = await http_get_json("https://api.example.com")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    @patch("exchanges.http_polling.urllib.request.urlopen")
    @patch("exchanges.http_polling.urllib.request.Request")
    async def test_user_agent_present(self, mock_request, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        await http_get_json("https://api.example.com")

        call_args = mock_request.call_args
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("User-Agent") == "CriptoParserV2/1.0"

    @pytest.mark.asyncio
    @patch("exchanges.http_polling.urllib.request.urlopen")
    @patch("exchanges.http_polling.urllib.request.Request")
    async def test_parse_float_decimal(self, mock_request, mock_urlopen):
        """json.loads должен парсить float-числа как Decimal."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"price": 100.5, "volume": 10.0}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = await http_get_json("https://api.example.com")
        assert isinstance(result["price"], Decimal)
        assert result["price"] == Decimal("100.5")
        assert isinstance(result["volume"], Decimal)
        assert result["volume"] == Decimal("10.0")


class TestHttpPostJson:
    @pytest.mark.asyncio
    @patch("exchanges.http_polling.urllib.request.urlopen")
    @patch("exchanges.http_polling.urllib.request.Request")
    async def test_posts_json_body(self, mock_request, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"token": "abc"}'
        mock_urlopen.return_value.__enter__.return_value = mock_response

        result = await http_post_json("https://api.example.com/bullet", data={})
        assert result == {"token": "abc"}

        call_args = mock_request.call_args
        assert call_args.kwargs["method"] == "POST"
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("Content-Type") == "application/json"
        assert call_args.kwargs["data"] == b"{}"
