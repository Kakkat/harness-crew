from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import uuid


class TriadError(Exception):
    pass


def uid(prefix="m"):
    return prefix + "-" + uuid.uuid4().hex[:16]


def now():
    return time.time()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fingerprint(workspace: str | Path, excludes=()):
    """Content fingerprint, including untracked files. Never follow symlinks."""
    root = Path(workspace).resolve()
    digest = hashlib.sha256()
    ignored = {".git", "__pycache__", *excludes}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ignored)
        for name in sorted(files + [d for d in dirs if (Path(directory) / d).is_symlink()]):
            p = Path(directory) / name
            digest.update(p.relative_to(root).as_posix().encode("utf-8") + b"\0")
            if p.is_symlink():
                digest.update(b"link:" + os.readlink(p).encode("utf-8"))
            else:
                with p.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            digest.update(b"\0")
    return digest.hexdigest()


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open("a+b")
        try:
            if self.path.stat().st_size == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            f.close()
            raise TriadError(f"Already owned: {self.path}") from exc
        self.file = f
        return self

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


class CappedLog:
    """One bounded file, drained even after its storage allowance is exhausted."""
    marker = b"\n[TRIAD: output truncated by configured storage limit]\n"

    def __init__(self, path, limit):
        self.path = Path(path)
        self.limit = max(int(limit), len(self.marker))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("ab", buffering=0)
        self.size = self.path.stat().st_size
        self.truncated = self.size >= self.limit

    def write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        if self.truncated:
            return
        room = self.limit - len(self.marker) - self.size
        chunk = data[:max(0, room)]
        self.file.write(chunk)
        self.size += len(chunk)
        if len(chunk) < len(data):
            self.file.write(self.marker)
            self.size += len(self.marker)
            self.truncated = True

    def close(self):
        self.file.close()
