"""Owner-only files for hook logs, audit logs and IPC sockets.

Hook logs contain prompts, tool input/output and paths. They are created
0600 inside 0700 directories, and rotated so they do not grow without bound.
Kept dependency-free: hook processes import it on every agent event.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
ROTATE_BYTES = 10 * 1024 * 1024


def ensure_private_dir(path: Path) -> Path:
    """Create path (0700 for new directories) and tighten the leaf directory."""
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        if path.stat().st_uid == os.getuid():
            path.chmod(PRIVATE_DIR_MODE)
    except OSError:
        pass
    return path


def rotate_if_large(path: Path, limit: int = ROTATE_BYTES) -> None:
    """Keep one previous generation as <name>.1 once the file reaches limit."""
    try:
        if path.stat().st_size < limit:
            return
        path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def append_private_line(path: Path, line: str, *, rotate_bytes: int = ROTATE_BYTES) -> None:
    ensure_private_dir(path.parent)
    rotate_if_large(path, rotate_bytes)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE_MODE)
    try:
        # Files created by older versions were world-readable.
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            handle.write(line)
    finally:
        if fd != -1:
            os.close(fd)


def private_runtime_dir() -> Path:
    """A short, per-user, owner-only directory for sockets whose state path is too long."""
    if sys.platform == "darwin":
        try:
            user_temp = os.confstr("CS_DARWIN_USER_TEMP_DIR")
        except (ValueError, OSError):
            user_temp = None
        if user_temp:
            return ensure_private_dir(Path(user_temp) / "sidepulse")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return ensure_private_dir(Path(runtime) / "sidepulse")
    directory = Path("/tmp") / f"sidepulse-{os.getuid()}"
    ensure_private_dir(directory)
    info = directory.lstat()
    if info.st_uid != os.getuid() or not directory.is_dir() or directory.is_symlink():
        raise OSError(f"{directory} is not a private directory owned by this user")
    return directory
