"""The file channel between a program writer and the device watcher.

Both sides see one directory: on TrueNAS it is /dev/shm shared through the
IPC namespace, so nothing touches a disk or a network. The writer replaces
PROGRAM atomically and touches it on every poll as a heartbeat; the watcher
answers in PROGRAM.result with what it did, keyed by a digest of the program
so a reply is never mistaken for an older one.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

RESULT_SUFFIX = ".result"
OUTCOMES = ("applied", "unconfirmed", "rejected", "write-failed")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def result_path(program_path: Path) -> Path:
    return program_path.with_name(program_path.name + RESULT_SUFFIX)


def replace_file(path: Path, text: str) -> None:
    """Write and rename, so a reader never sees a partial file."""

    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def touch(path: Path, now: float | None = None) -> None:
    """The heartbeat: a fresh mtime says the writer is alive, content aside."""

    stamp = time.time() if now is None else now
    os.utime(path, (stamp, stamp))


def age_seconds(path: Path, now: float | None = None) -> float | None:
    """Seconds since the last write or heartbeat; None when there is no file."""

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - mtime)


def write_result(program_path: Path, program_digest: str, outcome: str, detail: str = "") -> None:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    detail = " ".join(detail.split())
    replace_file(result_path(program_path), f"{outcome} {program_digest} {detail}".rstrip() + "\n")


def read_result(program_path: Path) -> tuple[str, str, str] | None:
    """(outcome, digest, detail) from the watcher's last answer, or None."""

    try:
        line = result_path(program_path).read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    parts = line.split(" ", 2)
    if len(parts) < 2 or parts[0] not in OUTCOMES:
        return None
    return parts[0], parts[1], parts[2] if len(parts) == 3 else ""


def await_result(program_path: Path, program_digest: str, timeout: float, *, interval: float = 0.25):
    """Poll for the watcher's answer to this program; None when it never comes."""

    deadline = time.monotonic() + timeout
    while True:
        result = read_result(program_path)
        if result is not None and result[1] == program_digest:
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)
