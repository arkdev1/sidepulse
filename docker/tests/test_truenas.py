import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from sidepulse_hw.program import validate
from sidepulse_truenas import collector
from sidepulse_truenas.__main__ import write_program
from sidepulse_truenas.collector import Facts, decide, in_window, with_brightness
from sidepulse_truenas.rpc import CertificateMismatch, Client, RpcError

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeTrueNAS:
    """A tiny WebSocket JSON-RPC server answering from a dict of handlers."""

    def __init__(self, handlers, *, tls_context=None, host="127.0.0.1", port=0):
        self.handlers = handlers
        self.calls = []
        self.tls = tls_context
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, port))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self.tls:
                try:
                    conn = self.tls.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    continue
            threading.Thread(target=self.session, args=(conn,), daemon=True).start()

    def session(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        key = [l.split(":", 1)[1].strip() for l in data.decode().split("\r\n")
               if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        try:
            while True:
                request = json.loads(self.read_frame(conn))
                self.calls.append(request["method"])
                handler = self.handlers.get(request["method"])
                if handler is None:
                    reply = {"jsonrpc": "2.0", "id": request["id"],
                             "error": {"code": -32601, "message": "Method not found"}}
                else:
                    reply = {"jsonrpc": "2.0", "id": request["id"], "result": handler(*request["params"])}
                # A notification first, which the client must skip.
                self.send(conn, {"jsonrpc": "2.0", "method": "collection_update", "params": {}})
                self.send(conn, reply)
        except (OSError, ValueError, IndexError):
            conn.close()

    @staticmethod
    def read_frame(conn):
        def take(n):
            out = b""
            while len(out) < n:
                chunk = conn.recv(n - len(out))
                if not chunk:
                    raise OSError("closed")
                out += chunk
            return out
        first, second = take(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", take(8))[0]
        mask = take(4)
        payload = take(length)
        if first & 0x0F == 0x8:
            raise OSError("closed")
        return bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode()

    @staticmethod
    def send(conn, obj):
        payload = json.dumps(obj).encode()
        if len(payload) < 126:
            header = bytes([0x81, len(payload)])
        elif len(payload) < 65536:
            header = bytes([0x81, 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([0x81, 127]) + struct.pack("!Q", len(payload))
        conn.sendall(header + payload)


NEW_API = {
    "auth.login_ex": lambda p: {"response_type": "SUCCESS" if p["api_key"] == "k" else "AUTH_ERR"},
    "system.version": lambda: "TrueNAS-25.10.0",
    "pool.query": lambda f, o: [{"name": "tank", "status": "ONLINE", "healthy": True},
                                {"name": "fast", "status": "DEGRADED", "healthy": False}],
    "alert.list": lambda: [
        {"level": "CRITICAL", "formatted": "Disk sdb failed\nmore", "dismissed": False},
        {"level": "WARNING", "formatted": "meh", "dismissed": False},
        {"level": "ERROR", "formatted": "old", "dismissed": True},
    ],
    "core.get_jobs": lambda f, o: [{"method": "replication.run"}, {"method": "app.pull_images"}],
    "update.status": lambda: {"code": "NORMAL", "status": {"new_version": {"version": "25.10.1"}}},
    "app.query": lambda f, o: [{"name": "immich", "upgrade_available": True},
                               {"name": "adguard", "upgrade_available": True},
                               {"name": "plex", "upgrade_available": False}],
}

OLD_API = {
    "auth.login_with_api_key": lambda key: key == "k",
    "update.check_available": lambda: {"status": "AVAILABLE", "version": "24.10.2"},
    "app.query": lambda f, o: [],
}


class RpcTests(unittest.TestCase):
    def client(self, server, **kwargs):
        client = Client(f"ws://127.0.0.1:{server.port}/api/current", **kwargs)
        client.connect()
        self.addCleanup(client.close)
        return client

    def test_collects_everything_from_a_current_truenas(self):
        client = self.client(FakeTrueNAS(NEW_API))
        collector.login(client, "sidepulse", "k")
        self.assertEqual(collector.health(client), ["pool fast DEGRADED", "alert CRITICAL: Disk sdb failed"])
        self.assertEqual(collector.running_jobs(client, collector.DEFAULT_ACTIVITY_JOBS), ["replication.run"])
        self.assertEqual(collector.available_updates(client), ["system 25.10.1", "apps adguard, immich"])

    def test_falls_back_to_older_api_methods(self):
        server = FakeTrueNAS(OLD_API)
        client = self.client(server)
        collector.login(client, "sidepulse", "k")
        self.assertEqual(collector.available_updates(client), ["system 24.10.2"])
        self.assertEqual(server.calls[:2], ["auth.login_ex", "auth.login_with_api_key"])

    def test_bad_key_is_refused(self):
        client = self.client(FakeTrueNAS(NEW_API))
        with self.assertRaises(PermissionError):
            collector.login(client, "sidepulse", "wrong")

    def test_rpc_errors_surface(self):
        client = self.client(FakeTrueNAS({}))
        with self.assertRaises(RpcError):
            client.call("pool.query")

    def test_large_messages(self):
        client = self.client(FakeTrueNAS({"big": lambda: "x" * 200_000}))
        self.assertEqual(len(client.call("big")), 200_000)


class PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cert, key = os.path.join(cls.tmp.name, "c.pem"), os.path.join(cls.tmp.name, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=nas",
                        "-days", "2", "-keyout", key, "-out", cert], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        cls.server = FakeTrueNAS(NEW_API, tls_context=context)
        with open(cert) as handle:
            der = ssl.PEM_cert_to_DER_cert(handle.read())
        cls.pin = hashlib.sha256(der).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def url(self):
        return f"wss://127.0.0.1:{self.server.port}/api/current"

    def test_matching_pin_connects(self):
        pin = ":".join(self.pin[i:i + 2] for i in range(0, 64, 2)).upper()
        client = Client(self.url(), cert_sha256=pin)
        client.connect()
        collector.login(client, "sidepulse", "k")
        client.close()

    def test_wrong_pin_refuses_before_sending_anything(self):
        before = len(self.server.calls)
        client = Client(self.url(), cert_sha256="00" * 32)
        with self.assertRaises(CertificateMismatch) as caught:
            client.connect()
        self.assertEqual(caught.exception.seen, self.pin)
        time.sleep(0.2)
        self.assertEqual(len(self.server.calls), before)

    def test_unpinned_self_signed_is_refused(self):
        with self.assertRaises(ssl.SSLError):
            Client(self.url()).connect()


class DecideTests(unittest.TestCase):
    def test_priorities(self):
        facts = Facts(critical=["pool tank DEGRADED"], activity=["replication.run"], updates=["apps immich"])
        self.assertEqual(decide(facts, night=False, night_shows_critical=False, unreachable=False)[0], "critical")
        self.assertEqual(decide(facts, night=True, night_shows_critical=False, unreachable=False)[0], "night")
        self.assertEqual(decide(facts, night=True, night_shows_critical=True, unreachable=False)[0], "critical")
        facts.critical = []
        self.assertEqual(decide(facts, night=False, night_shows_critical=False, unreachable=False)[0], "activity")
        facts.activity = []
        self.assertEqual(decide(facts, night=False, night_shows_critical=False, unreachable=False)[0], "update")
        facts.updates = []
        self.assertEqual(decide(facts, night=False, night_shows_critical=False, unreachable=False),
                         ("ok", "all good"))
        self.assertEqual(decide(None, night=False, night_shows_critical=False, unreachable=True)[0],
                         "unreachable")

    def test_night_window_wraps_midnight(self):
        def at(h, m):
            return time.struct_time((2026, 10, 16, h, m, 0, 4, 289, 0))
        for h, m, expected in ((22, 59, False), (23, 0, True), (2, 30, True), (6, 59, True), (7, 0, False)):
            self.assertEqual(in_window("23:00-07:00", at(h, m)), expected, (h, m))
        self.assertFalse(in_window("", at(23, 30)))

    def test_default_programs_are_valid_with_brightness(self):
        for program in collector.DEFAULT_PROGRAMS.values():
            for brightness in (255, 64, 0):
                validate(with_brightness(program, brightness), 2)
                validate(with_brightness(program, brightness), 8)
        self.assertEqual(with_brightness("#00FF00", 64), "brightness 64\n#00FF00")
        self.assertEqual(with_brightness("off", 64), "off")

    def test_program_file_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "LEDS.LED")
            from pathlib import Path
            write_program(Path(path), "#00FF00\n")
            write_program(Path(path), "off\n")
            self.assertEqual(Path(path).read_text(), "off\n")
            self.assertEqual(os.listdir(directory), ["LEDS.LED"])


if __name__ == "__main__":
    unittest.main()
