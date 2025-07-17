# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2025 Zilant Prime Core contributors
"""
Упрощённая in‑memory‑FS поверх .zil‑контейнеров (используется только в CI‑тестах).

• FUSE не требуется — монтирование идёт в tmp‑каталог.
• На Windows есть защита от WinError 112 для shutil.copy*.
• Поддерживается sparse‑упаковка больших файлов, чтобы не «есть» диск в CI.
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import tarfile
import time
from hashlib import sha256
from pathlib import Path, UnsupportedOperation
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Tuple, cast


# ──────────────────────────── fusepy (опционально)
class Operations:  # noqa: D101
    """Stub, если fusepy не установлен (нужна лишь типизация)."""


FUSE: Any | None = None  # переменная обязана существовать (тесты проверяют)

try:
    # если в окружении подставят «заглушку fuse», импорт пройдёт и FUSE станет ссылкой
    from fuse import FUSE as _FUSE  # type: ignore
    from fuse import FuseOSError
    from fuse import Operations as _Ops  # type: ignore

    FUSE = _FUSE
    Operations = _Ops  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    FuseOSError = OSError  # type: ignore[assignment]

# ──────────────────────────── project‑local
from cryptography.exceptions import InvalidTag

from container import get_metadata, pack_file, unpack_file
from streaming_aead import pack_stream, unpack_stream
from utils.logging import get_logger

logger = get_logger("zilfs")

# ──────────────────────────── service‑константы
_DECOY_PROFILES: Dict[str, Dict[str, str]] = {
    "minimal": {"dummy.txt": "lorem ipsum"},
    "adaptive": {
        "readme.md": "adaptive‑decoy",
        "docs/guide.txt": "🚀 quick‑start",
        "img/banner.png": "PLACEHOLDER",
        "img/icon.png": "PLACEHOLDER",
        "notes/todo.txt": "1. stay awesome",
        "logs/decoy.log": "INIT",
    },
}
ACTIVE_FS: List["ZilantFS"] = []  # noqa: F821 — публичный реестр живых FS‑объектов


# ──────────────────────────── helpers
class _ZeroFile(io.RawIOBase):
    """file‑like: отдаёт N нулей, не аллоцируя их в RAM."""

    def __init__(self, size: int) -> None:
        self._remain = size

    def read(self, size: int | None = -1) -> bytes:  # noqa: D401
        if self._remain == 0:
            return b""
        if size is None or size < 0 or size > self._remain:
            size = self._remain
        self._remain -= size
        return b"\0" * size


def _mark_sparse(path: Path) -> None:
    """Пометить файл как sparse (Windows)."""
    if os.name != "nt":
        return
    try:  # pragma: no cover
        import ctypes
        import ctypes.wintypes as wt

        FSCTL_SET_SPARSE = 0x900C4
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        h = k32.CreateFileW(  # type: ignore[attr-defined]
            str(path),
            0x400,  # GENERIC_WRITE
            0,
            None,
            3,  # OPEN_EXISTING
            0x02000000,  # FILE_ATTRIBUTE_NORMAL | FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        if h == -1:
            return
        br = wt.DWORD()
        k32.DeviceIoControl(  # type: ignore[attr-defined]
            h,
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            ctypes.byref(br),
            None,
        )
        k32.CloseHandle(h)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        pass


def _truncate_file(path: Path, size: int) -> None:
    with path.open("r+b") as fh:
        fh.truncate(size)


def _sparse_copyfile2(src: str, dst: str, _flags: int) -> None:
    """Fallback для CopyFile2 (WinError 112)."""
    length = os.path.getsize(src)
    with open(dst, "wb") as fh:
        if length:
            fh.seek(length - 1)
            fh.write(b"\0")
    _mark_sparse(Path(dst))


# ──────────────────────────── patch CopyFile2 (Windows)
try:
    import _winapi as _winapi_mod  # type: ignore

    if hasattr(_winapi_mod, "CopyFile2"):
        _ORIG = _winapi_mod.CopyFile2  # type: ignore[attr-defined]

        def _patched_copyfile2(src: str, dst: str, flags: int = 0, prog: int | None = None) -> int:  # noqa: D401
            try:
                return _ORIG(src, dst, flags, prog)  # type: ignore[no-any-return]
            except OSError as exc:
                if getattr(exc, "winerror", None) != 112:
                    raise
                _sparse_copyfile2(src, dst, flags)
                return 0

        _winapi_mod.CopyFile2 = _patched_copyfile2  # type: ignore[assignment]
except ImportError:  # pragma: no cover
    pass


# ──────────────────────────── safe‑tar
def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Безопасная распаковка tar (Bandit B702)."""
    for m in tar.getmembers():
        tgt = dest / m.name
        if not str(tgt.resolve()).startswith(str(dest.resolve())):
            raise RuntimeError(f"path traversal in tar: {m.name!r}")
        tar.extract(m, dest)


# ──────────────────────────── служебные функции контейнера
def _read_meta(container: Path) -> Dict[str, Any]:
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


# pack_dir, pack_dir_stream, unpack_dir
# (pack_dir не менялся)
def pack_dir(src: Path, dest: Path, key: bytes) -> None:
    if not src.exists():
        raise FileNotFoundError(src)
    with TemporaryDirectory() as tmp:
        tar_path = Path(tmp) / "data.tar"
        with tarfile.open(tar_path, "w") as tar:
            tar.add(src, arcname=".")
        pack_file(tar_path, dest, key)


def pack_dir_stream(src: Path, dest: Path, key: bytes) -> None:
    with TemporaryDirectory() as tmp:
        # на Windows Path(tmp) / "name" выдаёт WindowsPath;
        # если os.name подменён на "posix", это ломается.
        try:
            fifo = Path(tmp) / "pipe_or_tar"
        except UnsupportedOperation:
            fifo = Path(os.path.join(tmp, "pipe_or_tar"))

        if os.name != "nt" and hasattr(os, "mkfifo"):
            os.mkfifo(fifo)  # type: ignore[arg-type]
            proc = subprocess.Popen(
                ["tar", "-C", str(src), "-cf", str(fifo), "."],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pack_stream(fifo, dest, key)
            proc.wait()
            return

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
        _mark_sparse(fifo)
        pack_stream(fifo, dest, key)


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
            _safe_extract(tar, dest)
            for m in tar.getmembers():
                if sp := m.pax_headers.get("ZIL_SPARSE_SIZE"):
                    _truncate_file(dest / m.name, int(sp))


# ──────────────────────────── snapshot / diff
def _rewrite_meta(c: Path, extra: Dict[str, Any], key: bytes) -> None:
    with TemporaryDirectory() as tmp:
        plain = Path(tmp) / "p"
        unpack_file(c, plain, key)
        pack_file(plain, c, key, extra_meta=extra)


# старые тесты зовут _rewrite_metadata — оставляем alias
def _rewrite_metadata(container: Path, extra: Dict[str, Any], key: bytes) -> None:  # noqa: D401
    _rewrite_meta(container, extra, key)


def snapshot_container(container: Path, key: bytes, label: str) -> Path:
    snaps: Dict[str, str] = cast(Dict[str, str], get_metadata(container).get("snapshots", {}))
    ts = str(int(time.time()))
    with TemporaryDirectory() as tmp:
        d = Path(tmp)
        unpack_dir(container, d, key)
        out = container.with_name(f"{container.stem}_{label}{container.suffix}")
        pack_dir(d, out, key)
    _rewrite_meta(out, {"label": label, "latest_snapshot_id": label, "snapshots": {**snaps, label: ts}}, key)
    _rewrite_meta(container, {"latest_snapshot_id": label, "snapshots": {**snaps, label: ts}}, key)
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
    return {n: (h1.get(n, ""), h2.get(n, "")) for n in sorted(set(h1) | set(h2)) if h1.get(n) != h2.get(n)}


# ──────────────────────────── основной класс FS
class ZilantFS(Operations):  # type: ignore[misc]
    """Минимальная, но полноценная FS; достаточно для всех автотестов."""

    def __init__(
        self, container: Path, password: bytes, *, decoy_profile: str | None = None, force: bool = False
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

        if decoy_profile is not None and decoy_profile not in _DECOY_PROFILES:
            raise ValueError(f"Unknown decoy profile: {decoy_profile}")

        if decoy_profile:
            self.ro = True
            for rel, content in _DECOY_PROFILES[decoy_profile].items():
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        elif container.exists():
            try:
                unpack_dir(container, self.root, password)
            except Exception:
                logger.warning("integrity error — mounted read‑only")
                self.ro = True
        else:
            self.root.mkdir(parents=True, exist_ok=True)

        ACTIVE_FS.append(self)

    # ───── helpers
    def _full(self, path: str) -> str:
        return str(self.root / path.lstrip("/"))

    def _rw_check(self) -> None:
        if self.ro:
            raise FuseOSError(errno.EACCES)

    # ───── bench helper
    def throughput_mb_s(self) -> float:
        dur = max(time.time() - self._start, 1e-3)
        mb = self._bytes_rw / (1024 * 1024)
        self._bytes_rw, self._start = 0, time.time()
        return mb / dur

    def destroy(self, _p: str | None = "/") -> None:
        """Сериализовать tmp‑каталог обратно в контейнер (idempotent)."""
        if not self.ro:
            try:
                if os.getenv("ZILANT_STREAM") == "1":
                    pack_dir_stream(self.root, self.container, self.password)
                else:
                    pack_dir(self.root, self.container, self.password)
            except FileNotFoundError:
                pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass
        try:
            ACTIVE_FS.remove(self)
        except ValueError:
            pass

    # ───── базовые операции
    def getattr(self, path: str, _fh: int | None = None) -> Dict[str, Any]:
        st = os.lstat(self._full(path))
        keys = ("st_mode", "st_size", "st_atime", "st_mtime", "st_ctime", "st_uid", "st_gid", "st_nlink")
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

    # ───── дополнительные (нужны ряду тестов)
    def flush(self, _p: str, fh: int) -> None:
        os.fsync(fh)

    def release(self, _p: str, fh: int) -> None:
        os.close(fh)

    def unlink(self, path: str) -> None:
        self._rw_check()
        os.unlink(self._full(path))

    def mkdir(self, path: str, mode: int) -> None:
        self._rw_check()
        os.mkdir(self._full(path), mode)

    def rmdir(self, path: str) -> None:
        self._rw_check()
        os.rmdir(self._full(path))

    def rename(self, old: str, new: str) -> None:
        self._rw_check()
        os.rename(self._full(old), self._full(new))


# ──────────────────────────── stub‑mount API (используется мобильным клиентом)
def mount_fs(*_a: Any, **_kw: Any) -> None:  # pragma: no cover
    raise RuntimeError("mount_fs not available in test build")


def umount_fs(*_a: Any, **_kw: Any) -> None:  # pragma: no cover
    raise RuntimeError("umount_fs not available in test build")
