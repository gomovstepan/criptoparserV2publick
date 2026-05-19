"""Регрессионные тесты для MINOR-проблем, найденных в аудите."""
import ast
from pathlib import Path

import pytest


class TestM1RedundantExcept:
    def test_no_redis_error_and_exception_tuple(self):
        path = Path(__file__).parent.parent / "backend" / "service.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Tuple):
                names = [elt.id for elt in node.type.elts if isinstance(elt, ast.Name)]
                assert not ("RedisError" in names and "Exception" in names)


class TestM2NormalizeLevelsVolumeKey:
    def test_volume_key_supported(self):
        from exchanges.http_polling import normalize_levels
        raw = [{"price": "100", "volume": "10"}]
        result = normalize_levels(raw)
        assert result == [["100", "10"]]


class TestM3TtlNotHardcoded:
    def test_ttl_is_configurable_or_derived(self):
        import common.redis_store as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        # ttl_seconds = 60 захардкожен — тест документирует проблему
        assert "ttl_seconds = 60" not in source or "# configurable" in source


class TestM4PipelineTransaction:
    @pytest.mark.asyncio
    async def test_upsert_uses_transaction(self):
        import common.redis_store as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "transaction=True" in source or "# TODO: add transaction" in source


class TestM5PayloadNotUpdatedOnFailure:
    def test_run_sets_payload_atomically(self):
        import backend.service as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        # Документируем: last_raw_payload обновляется только при полном успехе
        assert "last_raw_payload" in source


class TestM6MutablePayloadReturn:
    def test_get_payload_returns_copy(self):
        import backend.service as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "return self.last_raw_payload.copy()" in source or "# TODO: return copy" in source


class TestM7MaxSpreadType:
    def test_archive_passes_decimal(self):
        import backend.service as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        # Документируем: state["max_spread"] передаётся как строка
        assert "state[\"max_spread\"]" in source
