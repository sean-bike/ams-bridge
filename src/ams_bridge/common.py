"""Shared record and path rules; no remote operations."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath



class BridgeError(Exception):
    def __init__(self, message: str, code: str = "invalid_input"):
        super().__init__(message)
        self.code = code


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_state(path):
    info = path.stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}


def read_json(path) -> dict:
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        with Path(path).open(encoding="utf-8") as stream:
            value = json.load(stream, object_pairs_hook=pairs,
                              parse_float=finite_float,
                              parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (OSError, ValueError) as exc:
        raise BridgeError(f"{path}: {exc}") from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + .5
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows briefly refuses replacement while another client reads JSON.
                if os.name != "nt" or exc.winerror not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def operation_lock(directory, name=".operation.lock"):
    path = local_file(Path(directory), name)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError(f"operation lock is linked or not regular: {path}", "unsafe_path")
    with path.open("a+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BridgeError(f"operation lock is linked or not regular: {path}", "unsafe_path")
        try:
            if os.name == "nt":
                import msvcrt
                if not info.st_size:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError(f"job has a conflicting operation: {directory}", "busy") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def relative(value: str, field="path") -> str:
    if (not isinstance(value, str) or not value or any(c in value for c in '\\:<>"|?*') or
            any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise BridgeError(f"{field}: expected a portable relative POSIX path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts:
        raise BridgeError(f"{field}: path escapes input root: {value}")
    if any(part.endswith((" ", ".")) or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", part)
           for part in p.parts):
        raise BridgeError(f"{field}: non-portable path: {value}")
    return p.as_posix()


def is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        # Includes Windows junctions on Python 3.11, before Path.is_junction existed.
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def local_file(root: Path, name: str) -> Path:
    name = relative(name)
    target = root / name
    cursor = target
    while True:
        if is_link(cursor):
            raise BridgeError(f"symbolic link / junction is not supported: {cursor}")
        if cursor == root:
            break
        cursor = cursor.parent
    if not target.resolve().is_relative_to(root.resolve()):
        raise BridgeError(f"path escapes input root: {name}")
    return target
