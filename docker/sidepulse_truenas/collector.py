"""Decide what the LEDs should show from TrueNAS state."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .rpc import METHOD_NOT_FOUND, Client, RpcError

CRITICAL_ALERT_LEVELS = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}
DEFAULT_ACTIVITY_JOBS = (
    "replication.run",
    "cloudsync.sync",
    "cloud_backup.sync",
    "rsynctask.run",
    "pool.scrub.scrub",
)

# Priority order, highest first. "night" and "unreachable" are added by decide().
STATES = ("night", "critical", "unreachable", "activity", "update", "ok")
DEFAULT_PROGRAMS = {
    "night": "off",
    "critical": "#FF0000 300ms none\noff 300ms none\nrepeat",
    "unreachable": "#8000FF 2s pulse\noff 2s none\nrepeat",
    "activity": "#0050FF 1.4s pulse\nrepeat",
    "update": "#FFB000 2s pulse\noff 1s none\nrepeat",
    "ok": "#00FF00",
}


@dataclass
class Facts:
    critical: list[str] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)


def login(client: Client, username: str, api_key: str) -> None:
    try:
        result = client.call(
            "auth.login_ex",
            {"mechanism": "API_KEY_PLAIN", "username": username, "api_key": api_key},
        )
        ok = isinstance(result, dict) and result.get("response_type") == "SUCCESS"
        detail = result.get("response_type") if isinstance(result, dict) else result
    except RpcError as exc:
        if exc.code != METHOD_NOT_FOUND:
            raise
        ok = client.call("auth.login_with_api_key", api_key) is True
        detail = "rejected"
    if not ok:
        raise PermissionError(f"TrueNAS login failed: {detail}")


def health(client: Client) -> list[str]:
    critical = []
    for pool in client.call("pool.query", [], {"select": ["name", "status", "healthy"]}) or []:
        if not pool.get("healthy", True) or pool.get("status") not in (None, "ONLINE"):
            critical.append(f"pool {pool.get('name')} {pool.get('status')}")
    for alert in client.call("alert.list") or []:
        if alert.get("dismissed"):
            continue
        if str(alert.get("level", "")).upper() in CRITICAL_ALERT_LEVELS:
            text = str(alert.get("formatted") or alert.get("klass") or "alert").splitlines()[0]
            critical.append(f"alert {alert.get('level')}: {text[:120]}")
    return critical


def running_jobs(client: Client, methods: tuple[str, ...]) -> list[str]:
    jobs = client.call("core.get_jobs", [["state", "=", "RUNNING"]], {"select": ["method"]}) or []
    found = sorted({str(job.get("method", "")) for job in jobs})
    return [m for m in found if any(m == p or m.startswith(p + ".") for p in methods)]


def available_updates(client: Client) -> list[str]:
    updates = []
    try:
        status = client.call("update.status") or {}
        new = (status.get("status") or {}).get("new_version")
        if new:
            updates.append(f"system {new.get('version')}")
    except RpcError as exc:
        if exc.code != METHOD_NOT_FOUND:
            raise
        status = client.call("update.check_available") or {}
        if status.get("status") == "AVAILABLE":
            updates.append(f"system {status.get('version', '')}".strip())
    apps = client.call("app.query", [], {"select": ["name", "upgrade_available"]}) or []
    names = sorted(str(app.get("name")) for app in apps if app.get("upgrade_available"))
    if names:
        updates.append("apps " + ", ".join(names))
    return updates


def in_window(window: str, now: time.struct_time) -> bool:
    """True when the local time is inside "HH:MM-HH:MM", which may wrap midnight."""

    if not window.strip():
        return False
    start, _, end = window.partition("-")
    def minutes(value: str) -> int:
        hours, _, mins = value.strip().partition(":")
        return int(hours) * 60 + int(mins or 0)
    begin, finish, current = minutes(start), minutes(end), now.tm_hour * 60 + now.tm_min
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    return current >= begin or current < finish


def decide(
    facts: Facts | None,
    *,
    night: bool,
    night_shows_critical: bool,
    unreachable: bool,
) -> tuple[str, str]:
    if facts is not None and facts.critical and (not night or night_shows_critical):
        return "critical", "; ".join(facts.critical)
    if night:
        return "night", "quiet hours"
    if unreachable or facts is None:
        return "unreachable", "TrueNAS API not reachable"
    if facts.activity:
        return "activity", "; ".join(facts.activity)
    if facts.updates:
        return "update", "; ".join(facts.updates)
    return "ok", "all good"


def with_brightness(program: str, brightness: int) -> str:
    if brightness >= 255 or program.strip() == "off":
        return program
    return f"brightness {brightness}\n{program}"
