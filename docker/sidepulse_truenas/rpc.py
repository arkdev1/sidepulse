"""Minimal JSON-RPC 2.0 client over WebSocket, standard library only.

TLS is mandatory for API keys (TrueNAS revokes keys sent in clear). With a
self-signed certificate, pin its SHA-256 fingerprint: the pin is checked right
after the TLS handshake, before anything, the API key included, is sent.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
METHOD_NOT_FOUND = -32601


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code


class TransportError(OSError):
    pass


class CertificateMismatch(TransportError):
    def __init__(self, seen: str) -> None:
        super().__init__(f"TLS certificate fingerprint is {seen}, not the pinned one")
        self.seen = seen


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def normalize_fingerprint(value: str) -> str:
    return value.replace(":", "").replace(" ", "").strip().lower()


def probe_fingerprint(host: str, port: int, timeout: float = 10.0) -> str:
    """Read the server certificate without trusting it or sending anything."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as sock:
            return fingerprint(sock.getpeercert(binary_form=True) or b"")


class Client:
    def __init__(self, url: str, *, cert_sha256: str = "", timeout: float = 30.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("wss", "ws"):
            raise ValueError("URL must start with wss://")
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "wss" else 80)
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.pin = normalize_fingerprint(cert_sha256)
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.next_id = 0

    # -- connection -------------------------------------------------------

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            self.sock = self._wrap(raw) if self.scheme == "wss" else raw
            self._handshake()
        except BaseException:
            raw.close()
            self.sock = None
            raise

    def _wrap(self, raw: socket.socket) -> socket.socket:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.pin:
            # The pin replaces CA and hostname checks, which a self-signed
            # NAS certificate cannot pass.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context.load_default_certs()
        sock = context.wrap_socket(raw, server_hostname=self.host)
        seen = fingerprint(sock.getpeercert(binary_form=True) or b"")
        if self.pin and seen != self.pin:
            sock.close()
            raise CertificateMismatch(seen)
        self.seen_fingerprint = seen
        return sock

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16))
        host = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
        request = (
            f"GET {self.path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key.decode()}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        while b"\r\n\r\n" not in self.buffer:
            self._fill()
            if len(self.buffer) > 65536:
                raise TransportError("WebSocket handshake response too large")
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise TransportError(f"WebSocket upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        expected = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise TransportError("WebSocket handshake failed")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
            self.sock.close()
            self.sock = None
            self.buffer = b""

    # -- framing ----------------------------------------------------------

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise TransportError("connection closed")
        self.buffer += chunk

    def _take(self, length: int) -> bytes:
        while len(self.buffer) < length:
            self._fill()
        data, self.buffer = self.buffer[:length], self.buffer[length:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _recv_message(self) -> str:
        parts: list[bytes] = []
        total = 0
        while True:
            first, second = self._take(2)
            opcode, final = first & 0x0F, first & 0x80
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._take(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8))[0]
            mask = self._take(4) if second & 0x80 else b""
            total += length
            if total > MAX_MESSAGE_BYTES:
                raise TransportError("message too large")
            payload = self._take(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise TransportError("server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                total -= length
                continue
            if opcode == 0xA:
                total -= length
                continue
            parts.append(payload)
            if final:
                return b"".join(parts).decode("utf-8")

    # -- JSON-RPC ---------------------------------------------------------

    def call(self, method: str, *params):
        self.next_id += 1
        request_id = self.next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": list(params)}
        self._send_frame(0x1, json.dumps(message).encode())
        while True:
            reply = json.loads(self._recv_message())
            if reply.get("id") != request_id:
                continue  # notifications and stale replies
            if "error" in reply:
                error = reply["error"] or {}
                data = error.get("data") or {}
                reason = data.get("reason") if isinstance(data, dict) else None
                raise RpcError(int(error.get("code", 0)), reason or str(error.get("message", "error")))
            return reply.get("result")
