# SPDX-FileCopyrightText: 2025 Zilant Prime Core contributors
# SPDX-License-Identifier: MIT

import os
import sys
import platform
import importlib.util
from pathlib import Path

# вставляем папку src/ в начало sys.path, чтобы
# import zilant_prime_core… брал код именно из src/
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

os.environ.setdefault("ZILANT_ALLOW_ROOT", "1")

import pytest

@pytest.fixture(autouse=True)
def _disable_screen_guard(monkeypatch):
    """Skip screen guard checks during tests."""
    from zilant_prime_core.utils import screen_guard
    monkeypatch.setattr(screen_guard.guard, "assert_secure", lambda: None)
    yield

def pytest_runtest_setup(item):
    # пропускаем все тесты, в имени которых есть "pyside", если нет PySide6
    if "pyside" in item.nodeid.lower():
        if importlib.util.find_spec("PySide6") is None:
            pytest.skip("PySide6 is not installed, skipping UI tests")

    # пропускаем все tests/test_zilfs_* на не‑Windows платформах (нет _winapi)
    if item.nodeid.startswith("tests/test_zilfs") and platform.system() != "Windows":
        pytest.skip("Windows-only _winapi tests, skipping on non-Windows")
