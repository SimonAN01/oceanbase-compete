"""Private local state, atomic checkpoints and cross-process submission locks."""

import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import ToolError
from .transport import dumps, loads


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def fingerprint(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value):
        raise ToolError("Invalid state/profile name")
    return value


def read_json(path):
    try:
        if Path(path).stat().st_size > 32_000_000:
            raise ToolError("Local state file is too large")
        return loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("Cannot read a valid JSON state file", path=str(path)) from exc


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".obcomp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(dumps(value, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Store:
    def __init__(self, root=None):
        if root is None:
            root = os.environ.get("OBCOMP_STATE_DIR")
        if root is None:
            root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "state")) / "oceanbase-compete"
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, kind, name):
        return self.root / slug(kind) / (slug(name) + ".json")

    def save(self, kind, name, data):
        path = self.path(kind, name)
        write_json(path, data)
        return str(path)

    def load(self, kind, name):
        return read_json(self.path(kind, name))

    def list(self, kind):
        return [read_json(p) for p in sorted((self.root / slug(kind)).glob("*.json"))]

    @contextmanager
    def lock(self, name):
        path = self.root / "locks" / (slug(name) + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        stream = os.fdopen(fd, "r+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ToolError("Another process owns this operation's lock") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ToolError("Another process owns this operation's lock") from exc
            locked = True
            yield
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream, fcntl.LOCK_UN)
            stream.close()
