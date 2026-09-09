from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from url_crawler_service import SERVICE_EXTRA_HINT

EXIT_CONFIG = 2


def import_without(name: str, missing: str) -> ModuleType:
    """Load a private copy of a module with one of the service extra's packages unavailable."""
    spec = importlib.util.find_spec(name)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, missing, None)
        for cached in [name for name in sys.modules if name.startswith("url_crawler_service")]:
            patch.delitem(sys.modules, cached)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "missing"),
    [("url_crawler_service.api", "fastapi"), ("url_crawler_service.worker", "sqlalchemy")],
)
def test_main_explains_a_missing_service_extra(
    name: str, missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    entrypoint = import_without(name, missing)

    assert entrypoint.main() == EXIT_CONFIG
    assert SERVICE_EXTRA_HINT in capsys.readouterr().err
