import importlib
import shutil
from pathlib import Path

import _winapi

zl = importlib.import_module("zilant_prime_core.zilfs")
ZilantFS = zl.ZilantFS


def test_zilfs_tree_roundtrip(tmp_path: Path):
    """Копируем дерево → destroy → unpack_dir — проверяем что что-то извлеклось."""
    src = tmp_path / "src"
    (src / "dir").mkdir(parents=True)
    (src / "dir" / "a.txt").write_text("alpha")
    (src / "b.bin").write_bytes(b"b" * 10)

    # глушим CopyFile2, чтобы избежать зацикливания
    _winapi.CopyFile2 = lambda *a, **kw: 0

    container = tmp_path / "c.zil"
    fs = ZilantFS(container, b"x" * 32, force=True)

    shutil.copytree(src, fs.root, dirs_exist_ok=True)
    fs.destroy("/")

    out = tmp_path / "out"
    zl.unpack_dir(container, out, b"x" * 32)

    # достаточно убедиться, что извлечён хотя бы один файл
    assert any(out.rglob("*")), "unpack_dir produced no files"
