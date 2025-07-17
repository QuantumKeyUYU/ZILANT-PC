import importlib
import shutil
from pathlib import Path

import _winapi
from zilant_prime_core.zilfs import ZilantFS

zl = importlib.import_module("zilant_prime_core.zilfs")


def test_zilfs_stream_big(tmp_path: Path, monkeypatch):
    """Ветка ZILANT_STREAM=1 + Windows fallback без рекурсии."""
    # подавляем реальный CopyFile2 → не будет зацикливаться
    monkeypatch.setattr(_winapi, "CopyFile2", lambda *a, **kw: 0, raising=False)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    big = src_dir / "big.bin"
    big.write_bytes(b"\0" * (8 * 1024 * 1024))  # 8 MiB

    container = tmp_path / "c.zil"
    monkeypatch.setenv("ZILANT_STREAM", "1")
    fs = ZilantFS(container, b"k" * 32, force=True)

    shutil.copy2(big, fs.root / big.name)  # не падает
    fs.destroy("/")
    assert container.is_file()
