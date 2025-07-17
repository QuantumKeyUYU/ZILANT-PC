# SPDX-FileCopyrightText: 2025 Zilant Prime Core contributors
# SPDX-License-Identifier: MIT
"""
Общий conftest:
* создаёт stub‑модули PySide6 / _winapi, если их нет;
* автоматически пропускает GUI‑тесты без PySide6 и Windows‑тесты без _winapi;
* отключает «screen guard» в автотестах.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import pytest
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

# ──────────────────────────── добавить src/ в PYTHONPATH
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

# Разрешаем юнит‑тесты от root в CI
os.environ.setdefault("ZILANT_ALLOW_ROOT", "1")


# ──────────────────────────── screen guard off
@pytest.fixture(autouse=True)
def _disable_screen_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Подменяем проверку «экран заблокирован» на no‑op."""
    from zilant_prime_core.utils import screen_guard

    monkeypatch.setattr(screen_guard.guard, "assert_secure", lambda: None)
    yield


# ──────────────────────────── хелпер для создания stub-модулей
def _ensure_stub(fullname: str, attrs: dict[str, Any] | None = None) -> ModuleType:
    """
    Создать пустой модуль (и подпакеты) в sys.modules, если он отсутствует.
    Возвращает сам модуль.
    """
    if fullname in sys.modules:
        return sys.modules[fullname]

    parent_name, _, short = fullname.rpartition(".")
    if parent_name:
        parent = _ensure_stub(parent_name)
    else:
        parent = None

    mod = ModuleType(fullname)
    if attrs:
        mod.__dict__.update(attrs)
    sys.modules[fullname] = mod
    if parent is not None and not hasattr(parent, short):
        setattr(parent, short, mod)
    return mod


# ──────────────────────────── stubs для зависимостей
# 1) PySide6 (+ QtCore/QtGui/QtWidgets) — чтобы не падал pytest‑qt
if importlib.util.find_spec("PySide6") is None:
    pyside = _ensure_stub("PySide6")
    _ensure_stub("PySide6.QtCore")
    _ensure_stub("PySide6.QtGui")
    _ensure_stub("PySide6.QtWidgets")
    # укажем минимальную "версию" для pytest-qt
    pyside.__version__ = "stub-0.0"

# 2) _winapi — чтобы tests/test_zilfs_* не падали на не‑Windows
if importlib.util.find_spec("_winapi") is None:
    _ensure_stub("_winapi")


# ──────────────────────────── динамический skip
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Отфильтровать тесты, если окружение не подходит."""
    gui_missing = importlib.util.find_spec("PySide6.QtCore") is None
    non_windows = platform.system() != "Windows"

    for item in items:
        name = item.nodeid.lower()
        if ("pyside" in name or "qt" in name) and gui_missing:
            item.add_marker(pytest.mark.skip(reason="GUI tests skipped (no PySide6)"))
        elif name.startswith("tests/test_zilfs") and non_windows:
            item.add_marker(pytest.mark.skip(reason="Windows‑only test skipped on non‑Windows"))
