# SPDX-FileCopyrightText: 2025 Zilant Prime Core contributors
# SPDX-License-Identifier: MIT
"""
conftest.py — stub‑модули и динамический skip для CI‑матрицы.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import platform
import pytest
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

# ─────────── вставляем src/ в начало PYTHONPATH
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

# разрешаем root?
os.environ.setdefault("ZILANT_ALLOW_ROOT", "1")


# ─────────── helper для stubs
def _ensure_stub(fullname: str, attrs: dict[str, Any] | None = None) -> ModuleType:
    if fullname in sys.modules:
        return sys.modules[fullname]
    parent_name, _, short = fullname.rpartition(".")
    parent = _ensure_stub(parent_name) if parent_name else None
    mod = ModuleType(fullname)
    # дать ему валидный __spec__
    mod.__spec__ = importlib.machinery.ModuleSpec(fullname, loader=None)
    if attrs:
        mod.__dict__.update(attrs)
    sys.modules[fullname] = mod
    if parent and not hasattr(parent, short):
        setattr(parent, short, mod)
    return mod


# ─────────── stub PySide6 (чтобы pytest-qt не падал)
if importlib.util.find_spec("PySide6") is None:
    _ensure_stub("PySide6")
    _ensure_stub("PySide6.QtCore")
    _ensure_stub("PySide6.QtGui")
    _ensure_stub("PySide6.QtWidgets")


# ─────────── stub _winapi (чтобы windows‑only тесты могли пропускаться)
if importlib.util.find_spec("_winapi") is None:
    _ensure_stub("_winapi")


# ───────────────────── отключаем «screen guard»
@pytest.fixture(autouse=True)
def _disable_screen_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from zilant_prime_core.utils import screen_guard

    monkeypatch.setattr(screen_guard.guard, "assert_secure", lambda: None)
    yield


# ───────────────────── динамический skip
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    gui_missing = importlib.util.find_spec("PySide6.QtCore") is None
    non_windows = platform.system() != "Windows"

    for item in items:
        nid = item.nodeid.lower()
        # любые тесты, где в path встречается «pyside» или «qt»
        if ("pyside6" in nid or "qt" in nid) and gui_missing:
            item.add_marker(pytest.mark.skip(reason="GUI tests skipped (no PySide6)"))
        # Windows‑only (zfs) тесты на non‑Windows
        if nid.startswith("tests/test_zilfs") and non_windows:
            item.add_marker(pytest.mark.skip(reason="Windows‑only tests skipped"))
