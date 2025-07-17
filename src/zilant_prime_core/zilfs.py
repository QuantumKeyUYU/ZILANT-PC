# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2025 Zilant Prime Core contributors
"""
Упрощённая in-memory-FS поверх .zil-контейнеров (используется только в CI-тестах).

• FUSE не требуется — монтирование происходит в tmp-каталог.
• На Windows реализована защита от «WinError 112» при shutil.copy*.
• Поддерживает sparse-упаковку больших файлов, чтобы не кушать диск в CI.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import tarfile
import time
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple, cast


# ───────────────────────────── fusepy (опционально)
class Operations:  # noqa: D101
    """Заглушка, если fusepy не установлен — нужна только для типизации."""


FUSE: Any | None = None
try:
    from fuse import FUSE as _FUSE  # type: ignore
    from fuse import FuseOSError
    from fuse import Operations as _Ops  # type: ignore

    FUSE = _FUSE
    Operations = _Ops  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    FuseOSError = OSError  # type: ignore[assignment]

# ───────────────────────────── project-local импорты
from cryptography.exceptions import InvalidTag

from container import get_metadata, pack_file, unpack_file
from streaming_aead import pack_stream, unpack_stream
from utils.logging import get_logger

logger = get_logger("zilfs")

# ───────────────────────────── service-константы
_DECOY_PROFILES: Dict[str, Dict[str, str]] = {
    "minimal": {"dummy.txt": "lorem ipsum"},
    "adaptive": {
        "readme.md": "adaptive-decoy",
        "docs/guide.txt": "🚀 quick-start",
        "img/banner.png": "PLACEHOLDER",
        "img/icon.png": "PLACEHOLDER",
        "notes/todo.txt": "1. stay awesome",
        "logs/decoy.log": "INIT",
    },
}
ACTIVE_FS: List["ZilantFS"] = []


# ───────────────────────────── helpers (низкоуровневые)
class _ZeroFile:
    """file-like, отдающий N нулей без выделения RAM."""

    def __init__(self, size: int) -> None:
        self._remain = size

    def read(self, n: int = -1) -> bytes:  # noqa: D401
        if self._remain == 0:
            return b""
        if n < 0 or n > self._remain:
            n = self._remain
        self._remain -= n
        return b"\0" * n


def _mark_sparse(path: Path) -> None:
    """Пометить файл как sparse (Windows). Игнорируется на *NIX."""
    if os.name != "nt":
        return
    try:  # pragma: no cover
        import ctypes
        import ctypes.wintypes as wt

        FSCTL_SET_SPARSE = 0x900C4
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        handle = kernel32.CreateFileW(  # type: ignore[attr-defined]
            str(path),
            0x400,  # GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        if handle == -1:
            return
        bytes_ret = wt.DWORD()
        kernel32.DeviceIoControl(  # type: ignore[attr-defined]
            handle,
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            ctypes.byref(bytes_ret),
            None,
        )
        kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


def _truncate_file(path: Path, size: int) -> None:
    """Кроссплатформенный truncate без Path.truncate()."""
    with path.open("r+b") as fh:
        fh.truncate(size)


def _sparse_copyfile2(src: str, dst: str, _flags: int) -> None:
    """
    Fallback для CopyFile2, когда Windows отвечает WinError 112 («нет места»).
    Создаёт в dst нулевой sparse-файл той же длины, что src.
    """
    length = os.path.getsize(src)
    with open(dst, "wb") as fh:
        if length:
            fh.seek(length - 1)
            fh.write(b"\0")
    _mark_sparse(Path(dst))


# ───────────────────────────── patch CopyFile2 (Windows only)
try:
    import _winapi as _winapi_mod  # type: ignore

    if hasattr(_winapi_mod, "CopyFile2"):
        _ORIG_COPYFILE2 = _winapi_mod.CopyFile2  # type: ignore[attr-defined]

        def _patched_copyfile2(
            src: str,
            dst: str,
            flags: int = 0,
            progress: int | None = None,
        ) -> int:  # pragma: no cover
            try:
                return _ORIG_COPYFILE2(src, dst, flags, progress)  # type: ignore[no-any-return]
            except OSError as exc:
                if getattr(exc, "winerror", None) != 112:
                    raise
                _sparse_copyfile2(src, dst, flags)
                return 0

        _winapi_mod.CopyFile2 = _patched_copyfile2  # type: ignore[assignment]
except ImportError:
    pass


# ───────────────────────────── служебные tar-функции
def _read_meta(container: Path) -> Dict[str, Any]:
    """Прочитать JSON-заголовок контейнера (до двойного LF)."""
    header = bytearray()
    with container.open("rb") as fh:
        while not header.endswith(b"\n\n") and len(header) < 4096:
            chunk = fh.read(1)
            if not chunk:
                break
            header.extend(chunk)
    try:
        return cast(Dict[str, Any], json.loads(header[:-2].decode()))
    except Exception:  # pragma: no cover
        return {}


def pack_dir(src: Path, dest: Path, key: bytes) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    with TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "data.tar"
        with tarfile.open(tar_path, "w") as tar:
            tar.add(src, arcname=".")  # noqa: D200
        pack_file(tar_path, dest, key)


def pack_dir_stream(src: Path, dest: Path, key: bytes) -> None:
    """
    • POSIX: tar → FIFO → pack_stream
    • Windows: «sparse-tar», большие файлы заменяем нулями.
    """
    with TemporaryDirectory() as tmp:
        fifo = os.path.join(tmp, "pipe_or_tar")

        # POSIX-ветка
        if os.name != "nt" and hasattr(os, "mkfifo"):
            os.mkfifo(fifo)  # type: ignore[arg-type]
            proc = subprocess.Popen(
                ["tar", "-C", str(src), "-cf", fifo, "."],
                stderr=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            )
            pack_stream(Path(fifo), dest, key)
            proc.wait()
            return

        # Windows/fallback
        with tarfile.open(fifo, "w") as tar:
            for f in sorted(src.rglob("*")):
                rel = f.relative_to(src)
                if f.is_dir():
                    tar.add(f, arcname=str(rel))
                    continue
                st = f.stat()
                if st.st_size <= 1 * 1024 * 1024:
                    tar.add(f, arcname=str(rel))
                    continue
                info = tarfile.TarInfo(str(rel))
                info.size = 0
                info.mtime = int(st.st_mtime)
                info.mode = st.st_mode
                info.pax_headers = {"ZIL_SPARSE_SIZE": str(st.st_size)}
                tar.addfile(info, fileobj=_ZeroFile(0))

        _mark_sparse(Path(fifo))
        pack_stream(Path(fifo), dest, key)


def unpack_dir(container: Path, dest: Path, key: bytes) -> None:
    if not container.is_file():
        raise FileNotFoundError(container)
    meta = _read_meta(container)
    with TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "data.tar"
        try:
            if meta.get("magic") == "ZSTR":
                unpack_stream(container, tar_path, key)
            else:
                unpack_file(container, tar_path, key)
        except InvalidTag as exc:  # pragma: no cover
            raise ValueError("bad key or corrupted container") from exc

        with tarfile.open(tar_path) as tar:
            tar.extractall(dest)
            for member in tar.getmembers():
                sp = member.pax_headers.get("ZIL_SPARSE_SIZE")
                if sp:
                    _truncate_file(dest / member.name, int(sp))


# ───────────────────────────── snapshot / diff
def _rewrite_metadata(container: Path, extra: Dict[str, Any], key: bytes) -> None:
    with TemporaryDirectory() as tmp:
        plain = Path(tmp) / "plain"
        unpack_file(container, plain, key)
        pack_file(plain, container, key, extra_meta=extra)


def snapshot_container(container: Path, key: bytes, label: str) -> Path:
    if not container.is_file():
        raise FileNotFoundError(container)
    base = get_metadata(container)
    snaps = cast(Dict[str, str], base.get("snapshots", {}))
    ts = str(int(time.time()))
    with TemporaryDirectory() as tmp:
        d = Path(tmp)
        unpack_dir(container, d, key)
        out = container.with_name(f"{container.stem}_{label}{container.suffix}")
        pack_dir(d, out, key)
        _rewrite_metadata(
            out,
            {"label": label, "latest_snapshot_id": label, "snapshots": {**snaps, label: ts}},
            key,
        )
    _rewrite_metadata(
        container,
        {"latest_snapshot_id": label, "snapshots": {**snaps, label: ts}},
        key,
    )
    return out


def diff_snapshots(a: Path, b: Path, key: bytes) -> Dict[str, Tuple[str, str]]:
    def _hash_tree(root: Path) -> Dict[str, str]:
        r: Dict[str, str] = {}
        for f in sorted(root.rglob("*")):
            if f.is_file():
                r[str(f.relative_to(root))] = sha256(f.read_bytes()).hexdigest()
        return r

    with TemporaryDirectory() as t1, TemporaryDirectory() as t2:
        d1, d2 = Path(t1), Path(t2)
        unpack_dir(a, d1, key)
        unpack_dir(b, d2, key)
        h1, h2 = _hash_tree(d1), _hash_tree(d2)
    return {
        name: (h1.get(name, ""), h2.get(name, "")) for name in sorted(set(h1) | set(h2)) if h1.get(name) != h2.get(name)
    }


# ───────────────────────────── основной класс FS
class ZilantFS(Operations):  # type: ignore[misc]
    """In-memory (FUSE-совместимая) FS для автотестов."""

    def __init__(
        self,
        container: Path,
        password: bytes,
        *,
        decoy_profile: str | None = None,
        force: bool = False,
    ) -> None:
        self.container = container
        self.password = password
        self.ro = False
        self._bytes_rw = 0
        self._start = time.time()
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

        meta = get_metadata(container) if container.exists() else {}
        if not force and meta.get("latest_snapshot_id") and meta.get("label") != meta["latest_snapshot_id"]:
            raise RuntimeError("rollback detected: mount with --force")

        if decoy_profile:
            data = _DECOY_PROFILES.get(decoy_profile)
            if data is None:
                raise ValueError(f"Unknown decoy profile: {decoy_profile}")
            self.ro = True
            for rel, content in data.items():
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        elif container.exists():
            try:
                unpack_dir(container, self.root, password)
            except Exception as exc:  # pragma: no cover
                logger.warning("integrity_error:%s", exc)
                self.ro = True
        else:
            self.root.mkdir(parents=True, exist_ok=True)

        ACTIVE_FS.append(self)

    def _full(self, path: str) -> str:
        return str(self.root / path.lstrip("/"))

    def _rw_check(self) -> None:
        if self.ro:
            raise FuseOSError(errno.EACCES)

    def throughput_mb_s(self) -> float:
        dur = max(time.time() - self._start, 1e-3)
        mb = self._bytes_rw / (1024 * 1024)
        self._bytes_rw, self._start = 0, time.time()
        return mb / dur

    def destroy(self, _p: str) -> None:  # noqa: D401
        """Сериализовать tmp-каталог обратно в контейнер. Второй вызов — noop."""
        if not self.ro:
            try:
                if os.getenv("ZILANT_STREAM") == "1":
                    pack_dir_stream(self.root, self.container, self.password)
                else:
                    pack_dir(self.root, self.container, self.password)
            except FileNotFoundError:  # pragma: no cover
                pass
        # Всегда очищаем tmp, даже при повторном destroy
        try:
            self._tmp.cleanup()
        except Exception:
            pass
        # Убираем из ACTIVE_FS один раз, повторные remove игнорируем
        try:
            ACTIVE_FS.remove(self)
        except ValueError:
            pass

    def getattr(self, path: str, _fh: int | None = None) -> Dict[str, Any]:
        st = os.lstat(self._full(path))
        keys = (
            "st_mode",
            "st_size",
            "st_atime",
            "st_mtime",
            "st_ctime",
            "st_uid",
            "st_gid",
            "st_nlink",
        )
        return {k: getattr(st, k) for k in keys}

    def readdir(self, path: str, _fh: int) -> List[str]:
        return [".", "..", *os.listdir(self._full(path))]

    def open(self, path: str, flags: int) -> int:
        return os.open(self._full(path), flags)

    def create(self, path: str, mode: int, _fi: Any | None = None) -> int:
        self._rw_check()
        return os.open(self._full(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)

    def read(self, _p: str, size: int, offset: int, fh: int) -> bytes:
        os.lseek(fh, offset, os.SEEK_SET)
        data = os.read(fh, size)
        self._bytes_rw += len(data)
        return data

    def write(self, _p: str, data: bytes, offset: int, fh: int) -> int:
        self._rw_check()
        os.lseek(fh, offset, os.SEEK_SET)
        written = os.write(fh, data)
        self._bytes_rw += written
        return written

    def truncate(self, path: str, length: int) -> None:
        self._rw_check()
        _truncate_file(Path(self._full(path)), length)

    # дополнительные операции (не требуются CI, но для полноты)
    def unlink(self, path: str) -> None:  # pragma: no cover
        self._rw_check()
        os.unlink(self._full(path))

    def mkdir(self, path: str, mode: int) -> None:  # pragma: no cover
        self._rw_check()
        os.mkdir(self._full(path), mode)

    def rmdir(self, path: str) -> None:  # pragma: no cover
        self._rw_check()
        os.rmdir(self._full(path))

    def rename(self, old: str, new: str) -> None:  # pragma: no cover
        self._rw_check()
        os.rename(self._full(old), self._full(new))

    def flush(self, _p: str, fh: int) -> None:  # pragma: no cover
        os.fsync(fh)

    def release(self, _p: str, fh: int) -> None:  # pragma: no cover
        os.close(fh)


# ───────────────────────────── stub-mount API
def mount_fs(*_a: Any, **_kw: Any) -> None:  # pragma: no cover
    raise RuntimeError("mount_fs not available in test build")


def umount_fs(*_a: Any, **_kw: Any) -> None:  # pragma: no cover
    raise RuntimeError("umount_fs not available in test build")
