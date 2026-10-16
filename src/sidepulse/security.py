"""Local security policy: what SidePulse may do beyond driving local LEDs.

The policy lives in its own file, separate from settings.json (which the
status-bar UI rewrites), and is fail-closed: a missing, unreadable, or
malformed file means the most restrictive behaviour.

Every outbound HTTP request goes through open_url(), so local-only mode is
enforced at a single choke point.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .settings import default_config_dir

SECURITY_POLICY_FILENAME = "security.json"
ALLOW_NETWORK_ENV = "SIDEPULSE_ALLOW_NETWORK"


class NetworkDisabledError(OSError):
    """Raised instead of opening a connection while local-only mode is on."""


DEFAULT_SOURCE_LABEL = "SidePulse"


@dataclass(frozen=True)
class SecurityPolicy:
    local_only: bool = True
    # Name shown to linked phones and relay receivers instead of the hostname.
    source_label: str = DEFAULT_SOURCE_LABEL
    # When a write to LEDS.LED is denied (read-only volume, MDM/DLP storage
    # policy), fall back to the read-request channel (Pro) or USB vendor
    # commands (Dot). Off by default: those transports bypass the very policy
    # that denied the write.
    readonly_fallback: bool = False
    # Origins (scheme://host[:port]) reachable even in local-only mode, such
    # as a self-hosted bridge (`sidepulse bridge serve`). Exact match only.
    allowed_servers: tuple[str, ...] = ()


def default_security_policy_path() -> Path:
    return default_config_dir() / SECURITY_POLICY_FILENAME


def load_security_policy(path: Path | None = None) -> SecurityPolicy:
    defaults = SecurityPolicy()
    target = (path or default_security_policy_path()).expanduser()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    values = {}
    for name, default in vars(defaults).items():
        value = data.get(name, default)
        # Only values of the expected type are honoured; anything else keeps
        # the safe default.
        values[name] = value if type(value) is type(default) else default
    label = values["source_label"].strip()[:80]
    values["source_label"] = label or DEFAULT_SOURCE_LABEL
    values["allowed_servers"] = load_allowed_servers(data.get("allowed_servers"))
    return SecurityPolicy(**values)


def load_allowed_servers(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    origins = []
    for entry in value:
        origin = server_origin(entry) if isinstance(entry, str) else None
        if origin and origin not in origins:
            origins.append(origin)
    return tuple(origins)


def server_origin(url: str) -> str | None:
    """scheme://host[:port], lowercased, default port dropped; None if not an HTTP(S) URL."""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def source_label() -> str:
    return load_security_policy().source_label


def network_allowed(path: Path | None = None, *, url: str | None = None) -> bool:
    """Whether a connection may be opened; to `url` when given, otherwise to anywhere."""
    if os.environ.get(ALLOW_NETWORK_ENV) == "1":
        return True
    policy = load_security_policy(path)
    if not policy.local_only:
        return True
    origin = server_origin(url) if url else None
    return origin is not None and origin in policy.allowed_servers


def network_disabled_message(url: str | None = None) -> str:
    origin = server_origin(url) if url else None
    allow_server = (
        f" To allow only {origin}, add \"allowed_servers\": [\"{origin}\"] there." if origin else ""
    )
    return (
        "network access is disabled (local-only mode). Set \"local_only\": false in "
        f"{default_security_policy_path()} or run with {ALLOW_NETWORK_ENV}=1 to allow it once."
        + allow_server
    )


def require_network(url: str | None = None) -> None:
    if not network_allowed(url=url):
        raise NetworkDisabledError(network_disabled_message(url))


def open_url(request: urllib.request.Request | str, *, timeout: float):
    """The only way SidePulse opens an outbound connection."""
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    require_network(url)
    return urllib.request.urlopen(request, timeout=timeout)
