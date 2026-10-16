from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sidepulse import bridge, security
from sidepulse.links import LinkError, iter_sse_messages
from sidepulse.relay import RelayConfig, listen_for_relay_events, publish_relay_event

CHANNEL = "b" * 22


class BridgeTestCase(unittest.TestCase):
    """A real bridge on an ephemeral loopback port, used through the real client code."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        environ = patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop(security.ALLOW_NETWORK_ENV, None)

        self.server = bridge.make_server("127.0.0.1", 0)
        self.url = bridge.server_url(self.server, tls=False)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        policy = security.default_security_policy_path()
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({"allowed_servers": [self.url]}), encoding="utf-8")

    def wait_for_subscriber(self, channel: str = CHANNEL) -> None:
        deadline = time.monotonic() + 5
        while channel not in self.server.hub._channels:
            if time.monotonic() > deadline:
                self.fail("receiver never subscribed")
            time.sleep(0.01)

    def request(self, method: str, path: str, body: bytes = b"") -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body)
            return connection.getresponse().status
        finally:
            connection.close()


class RelayThroughBridgeTests(BridgeTestCase):
    def test_status_relays_between_computers_without_private_content(self) -> None:
        received = []
        arrived = threading.Event()
        stop = threading.Event()

        def on_event(provider, line, event_id):
            received.append((provider, line))
            arrived.set()

        receiver = RelayConfig(server=self.url, receiver_channel=CHANNEL)
        listener = threading.Thread(
            target=listen_for_relay_events, args=(receiver, stop, on_event), daemon=True,
        )
        listener.start()
        self.addCleanup(stop.set)
        self.wait_for_subscriber()

        sender = RelayConfig(server=self.url, outbound_channel=CHANNEL, machine_name="Work Mac")
        publish_relay_event(sender, "claude", {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "prompt": "deploy with password hunter2",
            "last_assistant_message": "Should I also rotate the API key?",
        })
        self.assertTrue(arrived.wait(5))
        provider, line = received[0]
        self.assertEqual(provider, "claude")
        self.assertEqual(line["sidepulse_status"], "waiting_for_input")
        self.assertEqual(line["sidepulse_relay_source"], "Work Mac")
        self.assertNotIn("hunter2", json.dumps(line))

    def test_publishing_to_a_server_not_in_the_policy_is_refused(self) -> None:
        other = RelayConfig(server="http://127.0.0.1:1", outbound_channel=CHANNEL)
        with patch("urllib.request.urlopen") as urlopen, self.assertRaises(LinkError):
            publish_relay_event(other, "claude", {"hook_event_name": "Stop"})
        urlopen.assert_not_called()

    def test_messages_only_reach_their_own_channel(self) -> None:
        inbox = self.server.hub.subscribe("c" * 22)
        self.assertEqual(self.request("POST", f"/api/leds/{CHANNEL}", b"hello"), 202)
        self.assertTrue(inbox.empty())


class BridgeProtocolTests(BridgeTestCase):
    def test_bad_paths_channels_and_sizes_are_rejected(self) -> None:
        self.assertEqual(self.request("GET", "/"), 404)
        self.assertEqual(self.request("POST", "/api/leds/short", b"x"), 404)
        self.assertEqual(self.request("POST", f"/api/leds/{CHANNEL}/extra", b"x"), 404)
        self.assertEqual(self.request("POST", f"/api/leds/{CHANNEL}?q=1", b"x"), 404)
        self.assertEqual(
            self.request("POST", f"/api/leds/{CHANNEL}", b"x" * (bridge.MAX_MESSAGE_BYTES + 1)),
            413,
        )
        self.assertEqual(self.request("POST", f"/api/leds/{CHANNEL}", b"\xff"), 400)

    def test_subscriber_limit(self) -> None:
        inboxes = [self.server.hub.subscribe(CHANNEL) for _ in range(bridge.MAX_SUBSCRIBERS_PER_CHANNEL)]
        self.assertNotIn(None, inboxes)
        self.assertIsNone(self.server.hub.subscribe(CHANNEL))
        self.assertEqual(self.request("GET", f"/api/leds/{CHANNEL}"), 503)

    def test_keepalive_comments_and_multiline_messages(self) -> None:
        with patch.object(bridge, "KEEPALIVE_SECONDS", 0.05):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
            self.addCleanup(connection.close)
            connection.request("GET", f"/api/leds/{CHANNEL}")
            response = connection.getresponse()
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            self.wait_for_subscriber()
            time.sleep(0.2)
            self.server.hub.publish(CHANNEL, "line one\nline two")
            self.assertEqual(next(iter_sse_messages(response)), "line one\nline two")


if __name__ == "__main__":
    unittest.main()
