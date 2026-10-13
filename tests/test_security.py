from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from sidepulse import security
from sidepulse.cli import cmd_sidepulse_link
from sidepulse.firmware import FirmwareError, download
from sidepulse.links import IOSLink, LinkError, send_ios_program
from sidepulse.relay import RelayConfig, listen_for_relay_events, publish_relay_event
from sidepulse.security import (
    NetworkDisabledError,
    SecurityPolicy,
    load_security_policy,
    network_allowed,
    open_url,
)
from sidepulse.update import update_installation


class LocalOnlyTestCase(unittest.TestCase):
    """Runs with an isolated config dir and without the suite-wide network override."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        environ = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(security.ALLOW_NETWORK_ENV, None)
        self.urlopen = patch("urllib.request.urlopen").start()
        self.addCleanup(patch.stopall)

    def write_policy(self, text: str) -> Path:
        path = security.default_security_policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class SecurityPolicyTests(LocalOnlyTestCase):
    def test_missing_policy_is_local_only(self) -> None:
        self.assertEqual(load_security_policy(), SecurityPolicy(local_only=True))
        self.assertFalse(network_allowed())

    def test_malformed_policy_fails_closed(self) -> None:
        for text in ("not json", "[]", '{"local_only": "false"}', '{"local_only": 0}'):
            with self.subTest(text=text):
                self.write_policy(text)
                self.assertFalse(network_allowed())

    def test_explicit_opt_out_allows_network(self) -> None:
        self.write_policy(json.dumps({"local_only": False}))
        self.assertTrue(network_allowed())

    def test_source_label_replaces_hostname(self) -> None:
        self.assertEqual(security.source_label(), security.DEFAULT_SOURCE_LABEL)
        self.write_policy(json.dumps({"source_label": "  Work Mac  "}))
        self.assertEqual(security.source_label(), "Work Mac")
        for value in ("", "   ", 42, None):
            with self.subTest(value=value):
                self.write_policy(json.dumps({"source_label": value}))
                self.assertEqual(security.source_label(), security.DEFAULT_SOURCE_LABEL)

    def test_environment_override_allows_network_once(self) -> None:
        with patch.dict(os.environ, {security.ALLOW_NETWORK_ENV: "1"}):
            self.assertTrue(network_allowed())
        with patch.dict(os.environ, {security.ALLOW_NETWORK_ENV: "yes"}):
            self.assertFalse(network_allowed())


class EgressBlockedTests(LocalOnlyTestCase):
    def test_open_url_never_reaches_urlopen(self) -> None:
        with self.assertRaises(NetworkDisabledError):
            open_url("https://example.com", timeout=1)
        self.urlopen.assert_not_called()

    def test_relay_publish_is_blocked(self) -> None:
        config = RelayConfig(server="https://bridge.sidepulse.io", outbound_channel="a" * 22)
        with self.assertRaises(LinkError):
            publish_relay_event(config, "claude", {"hook_event_name": "Stop", "prompt": "secret"})
        self.urlopen.assert_not_called()

    def test_relay_listener_stops_instead_of_retrying(self) -> None:
        config = RelayConfig(server="https://bridge.sidepulse.io", receiver_channel="a" * 22)
        thread = threading.Thread(
            target=listen_for_relay_events,
            args=(config, threading.Event(), lambda *args: None),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.urlopen.assert_not_called()

    def test_phone_push_is_blocked(self) -> None:
        link = IOSLink(
            name="Phone",
            token="a" * 64,
            server="https://bridge.sidepulse.io",
            linked_at="2026-01-01T00:00:00Z",
        )
        with self.assertRaises(LinkError):
            send_ios_program(link, "off")
        self.urlopen.assert_not_called()

    def test_firmware_download_is_blocked(self) -> None:
        with self.assertRaises(FirmwareError):
            download("https://raw.githubusercontent.com/x/y/main/firmware/v1/SHA256SUMS.txt")
        self.urlopen.assert_not_called()

    def test_link_command_refuses_before_creating_relay_config(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cmd_sidepulse_link(argparse.Namespace(relay_code=None))
        self.assertEqual(result, 1)
        self.assertIn("local-only", stderr.getvalue())
        self.assertFalse(any(Path(self.tmp.name).rglob("relay.json")))

    def test_update_never_downloads_installer(self) -> None:
        with patch("sidepulse.update.subprocess.run") as run, redirect_stderr(io.StringIO()):
            self.assertEqual(update_installation(), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
