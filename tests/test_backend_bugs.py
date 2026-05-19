"""
AST-анализ backend/main.py и backend/service.py на известные архитектурные баги.
"""
import ast
from pathlib import Path

import pytest

BACKEND_MAIN_PATH = Path(__file__).parent.parent / "backend" / "main.py"
BACKEND_SERVICE_PATH = Path(__file__).parent.parent / "backend" / "service.py"


@pytest.fixture(scope="module")
def main_ast():
    source = BACKEND_MAIN_PATH.read_text(encoding="utf-8")
    return ast.parse(source)


@pytest.fixture(scope="module")
def service_ast():
    source = BACKEND_SERVICE_PATH.read_text(encoding="utf-8")
    return ast.parse(source)


class TestTelegramNotifierInitBug:
    """
    CRITICAL (исправлено): TelegramNotifier ранее инициализировался без dedup_ttl_seconds.
    Теперь класс находится в backend/service.py с default=60.0.
    """

    def test_telegram_notifier_has_default_dedup_ttl(self, service_ast):
        for node in ast.walk(service_ast):
            if isinstance(node, ast.ClassDef) and node.name == "TelegramNotifier":
                init = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
                assert init, "TelegramNotifier.__init__ not found"
                args = init[0].args
                arg_names = [a.arg for a in args.args]
                assert "dedup_ttl_seconds" in arg_names
                defaults_start = len(args.args) - len(args.defaults)
                dedup_idx = arg_names.index("dedup_ttl_seconds")
                assert dedup_idx >= defaults_start, "dedup_ttl_seconds должен иметь default"

    def test_backendservice_passes_dedup_ttl(self, service_ast):
        for node in ast.walk(service_ast):
            if isinstance(node, ast.ClassDef) and node.name == "BackendService":
                init = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
                assert init, "BackendService.__init__ not found"
                for sub in ast.walk(init[0]):
                    if isinstance(sub, ast.Call):
                        if isinstance(sub.func, ast.Name) and sub.func.id == "TelegramNotifier":
                            keywords = {k.arg for k in sub.keywords}
                            assert "dedup_ttl_seconds" in keywords, "BackendService должен передавать dedup_ttl_seconds"


class TestModuleLevelInitialization:
    """
    CRITICAL (исправлено): backend/main.py больше не создаёт service/redis_client/history_store
    на уровне модуля при импорте.
    """

    def test_no_module_level_service_instantiation(self, main_ast):
        forbidden = {"service", "redis_client", "history_store", "store"}
        for node in main_ast.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assert target.id not in forbidden, f"Module-level '{target.id}' found — refactor needed"


class TestBroadExceptClauses:
    """
    MINOR: except (RedisError, Exception) избыточно. Проверяем service, т.к. логика там.
    """

    def test_no_redis_error_and_exception_together(self, service_ast):
        for node in ast.walk(service_ast):
            if isinstance(node, ast.ExceptHandler):
                if isinstance(node.type, ast.Tuple):
                    names = []
                    for elt in node.type.elts:
                        if isinstance(elt, ast.Name):
                            names.append(elt.id)
                    if "RedisError" in names and "Exception" in names:
                        pytest.fail("Found 'except (RedisError, Exception)' — too broad")
