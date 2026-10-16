"""sidepulse-truenas: show TrueNAS state on a SidePulse via sidepulse-hw."""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from sidepulse_hw.program import ProgramError, normalize, validate

from .collector import (
    DEFAULT_ACTIVITY_JOBS,
    DEFAULT_PROGRAMS,
    Facts,
    available_updates,
    decide,
    health,
    in_window,
    login,
    running_jobs,
    with_brightness,
)
import ssl

from .rpc import CertificateMismatch, Client, RpcError, probe_fingerprint

UNREACHABLE_AFTER = 3


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def log(message: str) -> None:
    print(f"truenas: {message}", flush=True)


def load_config() -> dict:
    host = env("TRUENAS_HOST")
    key = env("TRUENAS_API_KEY")
    if not host or not key:
        raise SystemExit("truenas: ERROR set TRUENAS_HOST and TRUENAS_API_KEY")
    brightness = int(env("BRIGHTNESS", "255"))
    if not 0 <= brightness <= 255:
        raise SystemExit("truenas: ERROR BRIGHTNESS must be 0-255")
    programs = {}
    for state, default in DEFAULT_PROGRAMS.items():
        text = os.environ.get(f"PROGRAM_{state.upper()}", "").strip() or default
        program = with_brightness(text, brightness)
        try:
            validate(program, 8)  # valid for both the Dot and the Pro
        except ProgramError as exc:
            raise SystemExit(f"truenas: ERROR PROGRAM_{state.upper()} is invalid: {exc}")
        programs[state] = normalize(program)
    jobs = tuple(j.strip() for j in env("ACTIVITY_JOBS", ",".join(DEFAULT_ACTIVITY_JOBS)).split(",") if j.strip())
    return {
        "url": env("TRUENAS_URL") or f"wss://{host}/api/current",
        "username": env("TRUENAS_USERNAME", "sidepulse"),
        "key": key,
        "pin": env("TRUENAS_CERT_SHA256"),
        "night": env("NIGHT", "23:00-07:00"),
        "night_shows_critical": env("NIGHT_SHOWS_CRITICAL", "false").lower() in ("1", "true", "yes"),
        "poll": float(env("POLL_SECONDS", "30")),
        "update_every": float(env("UPDATE_CHECK_MINUTES", "60")) * 60,
        "jobs": jobs,
        "programs": programs,
        "output": Path(env("SIDEPULSE_PROGRAM_FILE", "/program/LEDS.LED")),
        "brightness": brightness,
    }


def write_program(path: Path, text: str) -> None:
    """Atomic replace, so sidepulse-hw never reads half a program."""

    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".LEDS.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def stop(signum, _frame) -> None:
    log(f"INFO  stopping ({signal.Signals(signum).name})")
    sys.exit(0)


def main() -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    config = load_config()
    if not config["url"].startswith("wss://"):
        log("ERROR TRUENAS_URL must use wss://; TrueNAS revokes API keys sent without TLS")
        return 1
    log(f"INFO  starting: {config['url']} as {config['username']}, poll {config['poll']:g}s, "
        f"updates every {config['update_every'] / 60:g} min, night {config['night'] or 'off'}, "
        f"brightness {config['brightness']}, TZ {os.environ.get('TZ', 'UTC')}")
    if not config["pin"]:
        log("WARN  TRUENAS_CERT_SHA256 not set: the certificate must be signed by a public CA")

    client: Client | None = None
    facts: Facts | None = None
    failures = 0
    last_update_check = 0.0
    last_state = None
    while True:
        try:
            if client is None:
                client = Client(config["url"], cert_sha256=config["pin"])
                client.connect()
                login(client, config["username"], config["key"])
                version = client.call("system.version")
                log(f"INFO  connected to {version}")
            critical = health(client)
            activity = running_jobs(client, config["jobs"])
            updates = facts.updates if facts else []
            if time.monotonic() - last_update_check >= config["update_every"] or not last_update_check:
                updates = available_updates(client)
                last_update_check = time.monotonic()
                log(f"INFO  update check: {'; '.join(updates) or 'nothing new'}")
            facts = Facts(critical=critical, activity=activity, updates=updates)
            failures = 0
        except CertificateMismatch as exc:
            log(f"ERROR {exc}. If this is your NAS certificate, set TRUENAS_CERT_SHA256={exc.seen}")
            client, failures = None, failures + 1
        except ssl.SSLCertVerificationError as exc:
            hint = ""
            if client is not None:
                try:
                    seen = probe_fingerprint(client.host, client.port)
                    hint = f". If this is your NAS certificate, set TRUENAS_CERT_SHA256={seen}"
                except OSError:
                    pass
            log(f"ERROR TLS certificate not trusted ({exc.verify_message}){hint}")
            client, failures = None, failures + 1
        except PermissionError as exc:
            log(f"ERROR {exc}; check TRUENAS_USERNAME and TRUENAS_API_KEY")
            client, failures = None, failures + 1
        except (OSError, RpcError, ValueError) as exc:
            log(f"WARN  TrueNAS API error: {exc}")
            if client is not None:
                client.close()
            client, failures = None, failures + 1

        night = in_window(config["night"], time.localtime())
        state, reason = decide(
            facts if failures < UNREACHABLE_AFTER else None,
            night=night,
            night_shows_critical=config["night_shows_critical"],
            unreachable=failures >= UNREACHABLE_AFTER,
        )
        if (state, reason) != last_state:
            try:
                write_program(config["output"], config["programs"][state])
                log(f"INFO  LEDs -> {state}: {reason}")
                last_state = (state, reason)
            except OSError as exc:
                log(f"ERROR cannot write {config['output']}: {exc}")
        time.sleep(config["poll"])


if __name__ == "__main__":
    sys.exit(main())
