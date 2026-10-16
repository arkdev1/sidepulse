"""Self-hosted relay bridge: `sidepulse bridge serve`.

Implements the part of the SidePulse bridge protocol that computer-to-computer
relay uses, so relayed agent status never passes through a third party:

    POST /api/leds/{channel}   publish one message to the channel
    GET  /api/leds/{channel}   Server-Sent Events stream of the channel

Channels are the random relay codes printed by `sidepulse link`; whoever
knows a code can publish to and read from it. Nothing is stored: a message
reaches the subscribers connected at that moment, and is otherwise dropped.
The iOS app talks to its own bridge and cannot use this one.
"""
from __future__ import annotations

import argparse
import queue
import re
import ssl
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .links import LinkError
from .relay import clean_channel

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_MESSAGE_BYTES = 64 * 1024
MAX_CHANNELS = 1024
MAX_SUBSCRIBERS_PER_CHANNEL = 16
SUBSCRIBER_BACKLOG = 100
KEEPALIVE_SECONDS = 15.0
CHANNEL_PATH = re.compile(r"/api/leds/([^/?#]+)")


class ChannelHub:
    """In-memory fan-out from publishers to the subscribers of a channel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, set[queue.Queue[str]]] = {}

    def subscribe(self, channel: str) -> queue.Queue[str] | None:
        with self._lock:
            subscribers = self._channels.get(channel)
            if subscribers is None:
                if len(self._channels) >= MAX_CHANNELS:
                    return None
                subscribers = self._channels[channel] = set()
            if len(subscribers) >= MAX_SUBSCRIBERS_PER_CHANNEL:
                return None
            inbox: queue.Queue[str] = queue.Queue(SUBSCRIBER_BACKLOG)
            subscribers.add(inbox)
            return inbox

    def unsubscribe(self, channel: str, inbox: queue.Queue[str]) -> None:
        with self._lock:
            subscribers = self._channels.get(channel)
            if subscribers is None:
                return
            subscribers.discard(inbox)
            if not subscribers:
                del self._channels[channel]

    def publish(self, channel: str, message: str) -> int:
        with self._lock:
            subscribers = list(self._channels.get(channel, ()))
        delivered = 0
        for inbox in subscribers:
            try:
                inbox.put_nowait(message)
                delivered += 1
            except queue.Full:
                # A stalled subscriber loses messages rather than holding memory.
                pass
        return delivered


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, *, hub: ChannelHub | None = None) -> None:
        super().__init__(address, BridgeRequestHandler)
        self.hub = hub or ChannelHub()
        self.stopping = threading.Event()

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()


class BridgeRequestHandler(BaseHTTPRequestHandler):
    server: BridgeServer
    server_version = "SidePulseBridge"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:
        # The default log line contains the path, and the path is the secret.
        pass

    def channel(self) -> str | None:
        match = CHANNEL_PATH.fullmatch(self.path)
        if not match:
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        try:
            return clean_channel(match[1], required=True)
        except LinkError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return None

    def do_POST(self) -> None:
        channel = self.channel()
        if channel is None:
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(HTTPStatus.LENGTH_REQUIRED)
            return
        if length < 0 or length > MAX_MESSAGE_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            message = self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Message must be UTF-8.")
            return
        self.server.hub.publish(channel, message)
        self.send_response(HTTPStatus.ACCEPTED)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        channel = self.channel()
        if channel is None:
            return
        inbox = self.server.hub.subscribe(channel)
        if inbox is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Too many subscribers.")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    message = inbox.get(timeout=KEEPALIVE_SECONDS)
                except queue.Empty:
                    # Keeps clients' read timeouts from firing on a quiet channel.
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(sse_event(message))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            pass
        finally:
            self.server.hub.unsubscribe(channel, inbox)


def sse_event(message: str) -> bytes:
    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return ("".join(f"data: {line}\n" for line in lines) + "\n").encode("utf-8")


def make_server(
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    *,
    certfile: str | None = None,
    keyfile: str | None = None,
) -> BridgeServer:
    server = BridgeServer((bind, port))
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile, keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def server_url(server: BridgeServer, *, tls: bool) -> str:
    host, port = server.server_address[:2]
    if ":" in host:
        host = f"[{host}]"
    return f"{'https' if tls else 'http'}://{host}:{port}"


def cmd_bridge_serve(args: argparse.Namespace) -> int:
    if bool(args.cert) != bool(args.key):
        print("sidepulse bridge: --cert and --key must be used together.", file=sys.stderr)
        return 2
    try:
        server = make_server(args.bind, args.port, certfile=args.cert, keyfile=args.key)
    except (OSError, ssl.SSLError) as exc:
        print(f"sidepulse bridge: {exc}", file=sys.stderr)
        return 1
    url = server_url(server, tls=bool(args.cert))
    print(f"SidePulse bridge listening on {url}", flush=True)
    print("On each computer, allow it in security.json:", flush=True)
    print(f'  "allowed_servers": ["{url}"]', flush=True)
    print(f"and link with: SIDEPULSE_SERVER={url} sidepulse link", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()
    return 0


def add_bridge_parser(subparsers: argparse._SubParsersAction) -> None:
    bridge = subparsers.add_parser("bridge", help="Run a self-hosted relay bridge.")
    commands = bridge.add_subparsers(dest="bridge_command", required=True)
    serve = commands.add_parser(
        "serve",
        help="Relay agent status between your computers without a third-party server.",
    )
    serve.add_argument(
        "--bind",
        default=DEFAULT_BIND,
        help=f"Address to listen on. Default: {DEFAULT_BIND} (this computer only).",
    )
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Default: {DEFAULT_PORT}.")
    serve.add_argument("--cert", help="TLS certificate (PEM) to serve HTTPS.")
    serve.add_argument("--key", help="TLS private key (PEM) for --cert.")
    serve.set_defaults(func=cmd_bridge_serve)
