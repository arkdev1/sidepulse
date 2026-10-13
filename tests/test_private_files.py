from __future__ import annotations

import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path

from sidepulse.hook import write_hook_line
from sidepulse.ipc import HookEventServer
from sidepulse.private_files import append_private_line, private_runtime_dir


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class PrivateFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_hook_log_is_owner_only(self) -> None:
        log = self.root / "state" / "claude.jsonl"
        write_hook_line(log, {"hook_event_name": "Stop", "prompt": "private"})
        self.assertEqual(mode(log), 0o600)
        self.assertEqual(mode(log.parent), 0o700)

    def test_existing_world_readable_log_is_tightened(self) -> None:
        log = self.root / "claude.jsonl"
        log.write_text("old\n")
        log.chmod(0o644)
        self.root.chmod(0o755)
        append_private_line(log, "new\n")
        self.assertEqual(log.read_text(), "old\nnew\n")
        self.assertEqual(mode(log), 0o600)
        self.assertEqual(mode(self.root), 0o700)

    def test_large_log_rotates_to_single_backup(self) -> None:
        log = self.root / "claude.jsonl"
        append_private_line(log, "a" * 20 + "\n", rotate_bytes=10)
        append_private_line(log, "b\n", rotate_bytes=10)
        append_private_line(log, "c\n", rotate_bytes=10)
        self.assertEqual(log.read_text(), "b\nc\n")
        self.assertEqual((self.root / "claude.jsonl.1").read_text(), "a" * 20 + "\n")

    def test_event_socket_is_owner_only(self) -> None:
        # AF_UNIX paths are short; keep the socket near the root.
        directory = Path(tempfile.mkdtemp(prefix="sp-", dir="/tmp"))
        self.addCleanup(lambda: os.path.isdir(directory) and os.rmdir(directory))
        path = directory / "events.sock"
        server = HookEventServer(lambda provider, line: None, socket_path=path)
        server.start()
        try:
            self.assertTrue(stat.S_ISSOCK(path.stat().st_mode))
            self.assertEqual(mode(path), 0o600)
            self.assertEqual(mode(directory), 0o700)
        finally:
            server.stop()
            path.unlink(missing_ok=True)

    def test_runtime_dir_is_private(self) -> None:
        directory = private_runtime_dir()
        self.assertEqual(mode(directory), 0o700)
        self.assertEqual(directory.stat().st_uid, os.getuid())
        self.assertFalse(str(directory).startswith("/tmp/sidepulse-relay"))


if __name__ == "__main__":
    unittest.main()
