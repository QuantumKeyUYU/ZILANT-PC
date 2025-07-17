import importlib
import pytest
import sys
from pathlib import Path

import _winapi

zl = importlib.import_module("zilant_prime_core.zilfs")
ZilantFS = zl.ZilantFS


def test_import_without_fuse(monkeypatch):
    """Стр. 33-37: перезагрузка без fuse."""
    sys.modules.pop("zilant_prime_core.zilfs", None)
    monkeypatch.setitem(sys.modules, "fuse", None)
    import importlib as _imp

    _imp.reload(importlib.import_module("zilant_prime_core.zilfs"))


def test_mark_sparse_noop(tmp_path: Path):
    """Стр. 84."""
    zl._mark_sparse(tmp_path / "dummy")  # на *nix ничего не делает


def test_rollback_detect(monkeypatch, tmp_path: Path):
    """Стр. 160-161."""
    cont = tmp_path / "c.zil"
    cont.touch()
    monkeypatch.setattr(zl, "get_metadata", lambda *_: {"latest_snapshot_id": "A", "label": "B"})
    with pytest.raises(RuntimeError):
        ZilantFS(cont, b"p" * 32)  # force=False по умолчанию


def test_pack_dir_stream_windows(monkeypatch, tmp_path: Path):
    """Стр. 221-226 (Windows-fallback) + 374-375 CopyFile2-патч."""
    monkeypatch.setattr(zl.os, "name", "nt", raising=False)
    # CopyFile2 → заглушка, чтоб не рекурсировало
    monkeypatch.setattr(_winapi, "CopyFile2", lambda *a, **k: 0, raising=False)

    # заставляем pack_stream просто создавать целевой файл
    def fake_pack(_fifo, Out, _key):
        Out.touch()

    monkeypatch.setattr(zl, "pack_stream", fake_pack)

    src = tmp_path / "dir"
    src.mkdir()
    (src / "x").write_bytes(b"x")
    dst = tmp_path / "out.zil"
    zl.pack_dir_stream(src, dst, b"k" * 32)
    assert dst.exists()


def test_truncate_and_double_destroy(tmp_path: Path):
    """Стр. 234 и 251."""
    f = tmp_path / "file"
    f.write_bytes(b"123456789")
    zl._truncate_file(f, 4)
    assert f.read_bytes() == b"1234"

    fs = ZilantFS(tmp_path / "c.zil", b"x" * 32, force=True)
    fs.destroy("/")  # первый
    fs.destroy("/")  # второй – ветка повторного вызова


def test_mount_umount_stub():
    """Стр. 374-375 (stub-функции)."""
    with pytest.raises(RuntimeError):
        zl.mount_fs()
    with pytest.raises(RuntimeError):
        zl.umount_fs()
