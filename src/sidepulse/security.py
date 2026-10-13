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
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .settings import default_config_dir

SECURITY_POLICY_FILENAME = "security.json"
ALLOW_NETWORK_ENV = "SIDEPULSE_ALLOW_NETWORK"


class NetworkDisabledError(OSError):
    """Raised instead of opening a connection while local-only mode is on."""


@dataclass(frozen=True)
class SecurityPolicy:
    local_only: bool = True


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
        # Only real booleans are honoured; anything else keeps the safe default.
        values[name] = value if isinstance(value, bool) else default
    return SecurityPolicy(**values)


def network_allowed(path: Path | None = None) -> bool:
    if os.environ.get(ALLOW_NETWORK_ENV) == "1":
        return True
    return not load_security_policy(path).local_only


def network_disabled_message() -> str:
    return (
        "network access is disabled (local-only mode). Set \"local_only\": false in "
        f"{default_security_policy_path()} or run with {ALLOW_NETWORK_ENV}=1 to allow it once."
    )


def require_network() -> None:
    if not network_allowed():
        raise NetworkDisabledError(network_disabled_message())


def open_url(request: urllib.request.Request | str, *, timeout: float):
    """The only way SidePulse opens an outbound connection."""
    require_network()
    return urllib.request.urlopen(request, timeout=timeout)
