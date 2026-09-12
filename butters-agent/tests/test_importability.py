from __future__ import annotations

import importlib
from pathlib import Path


def test_package_is_importable_from_repository_pytest_configuration():
    modules = [
        importlib.import_module(name)
        for name in (
            "butters_agent.protocol",
            "butters_agent.engine",
            "butters_agent.client",
            "butters_agent.platform.fake",
            "butters_agent.platform.win32",
        )
    ]

    package_root = Path("butters-agent/src").resolve()
    assert all(
        Path(module.__file__).resolve().is_relative_to(package_root)
        for module in modules
    )
