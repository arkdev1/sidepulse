"""sidepulse-hw: validate, preview, write and watch LED programs."""
from __future__ import annotations

import argparse
import hashlib
import os
import signal
import sys
import time
from pathlib import Path

from .device import Device, DeviceError
from .program import ProgramError, normalize, preview, validate

DEFAULT_DEVICE = os.environ.get("SIDEPULSE_DEV", "/dev/sidepulse")


def read_source(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="ascii")


def cmd_validate(args) -> int:
    validate(read_source(args.program), args.leds)
    print("ok")
    return 0


def cmd_preview(args) -> int:
    for index, frame in enumerate(
        preview(read_source(args.program), args.leds, duration_ms=args.ms, step_ms=args.step)
    ):
        colors = " ".join(f"#{r:02X}{g:02X}{b:02X}" for r, g, b in frame)
        print(f"{index * args.step:>6} ms  {colors}")
    return 0


def cmd_write(args) -> int:
    device = Device(args.device)
    changed = device.write_program(read_source(args.program))
    print("written" if changed else "unchanged")
    return 0


def cmd_read(args) -> int:
    sys.stdout.write(Device(args.device).read_file(args.file))
    return 0


def watched_program() -> tuple[str | None, str]:
    """The program to show and where it came from, for the log."""

    inline = os.environ.get("SIDEPULSE_PROGRAM", "")
    if inline.strip():
        return inline, "SIDEPULSE_PROGRAM"
    path = Path(os.environ.get("SIDEPULSE_PROGRAM_FILE", "/program/LEDS.LED"))
    try:
        return path.read_text(encoding="ascii"), str(path)
    except FileNotFoundError:
        return None, f"{path} (missing)"
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path} (unreadable: {exc})"


MODELS = {"PULSEDOT": "SidePulse Dot", "SIDEPULSE": "SidePulse Pro"}


def describe(text: str) -> str:
    text = normalize(text)
    lines = text.strip().splitlines()
    first = lines[0] if lines else ""
    more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
    return f"{len(text.encode())} bytes, {first!r}{more}"


def report_applied(device: Device, expected_len: int) -> None:
    """Confirm from STATUS.TXT that the firmware picked up the write."""

    for _ in range(7):
        time.sleep(0.3)
        status = device.status()
        if "leds_len" not in status:
            log("WARN  STATUS.TXT has no leds_len; cannot confirm the firmware applied it")
            return
        applied = status["leds_len"] == str(expected_len)
        if applied:
            break
    detail = (f"leds_len={status.get('leds_len', '?')} leds_applies={status.get('leds_applies', '?')} "
              f"leds_event={status.get('leds_event', '?')}")
    if applied:
        log(f"INFO  firmware applied it ({detail})")
    else:
        log(f"WARN  firmware has not applied it after 2s ({detail})")


def write_and_report(device: Device, text: str, what: str) -> None:
    if device.write_program(text):
        log(f"INFO  wrote {what}: {describe(text)}")
        report_applied(device, len(normalize(text).encode()))
    else:
        log(f"INFO  {what} already on the device, nothing written")


def cmd_watch(args) -> int:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"INFO  sidepulse-hw starting (image {os.environ.get('SIDEPULSE_HW_COMMIT', 'local build')[:12]}, "
        f"device {args.device}, poll {args.poll:g}s)")
    try:
        device = Device(args.device)
    except DeviceError as exc:
        log(f"ERROR {exc}")
        return 1
    status = device.status()
    log(f"INFO  found {MODELS.get(device.label, device.label)}: {device.led_count} LEDs, serial {status.get('serial', '?')}, "
        f"firmware {status.get('app_version', '?')} ({status.get('app_build', '?')})")

    log(f"INFO  startup: LEDs off, then {args.start_delay:g}s pause")
    try:
        write_and_report(device, "off", "startup off")
    except DeviceError as exc:
        log(f"ERROR startup off failed: {exc}")
    time.sleep(args.start_delay)

    last_digest = None
    last_source = None
    while True:
        text, source = watched_program()
        if source != last_source:
            log(f"INFO  program source: {source}")
            last_source = source
        digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        if text is not None and digest != last_digest:
            try:
                write_and_report(device, text, "program")
                last_digest = digest
            except ProgramError as exc:
                log(f"ERROR program rejected, device unchanged: {exc}")
                last_digest = digest
            except DeviceError as exc:
                log(f"ERROR write failed, retrying in {args.poll:g}s: {exc}")
        time.sleep(args.poll)


def stop(signum, _frame) -> None:
    log(f"INFO  stopping ({signal.Signals(signum).name}); the LEDs keep their last program")
    sys.exit(0)


def log(message: str) -> None:
    print(f"sidepulse: {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sidepulse-hw")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, *, program=True, device=False, leds=False):
        sub = commands.add_parser(name, help=help_text)
        if program:
            sub.add_argument("program", help="Program file, or - for stdin.")
        if device:
            sub.add_argument("--device", default=DEFAULT_DEVICE)
        if leds:
            sub.add_argument("--leds", type=int, choices=(2, 8), default=8)
        sub.set_defaults(func=func)
        return sub

    add("validate", cmd_validate, "Check a program with the firmware parser.", leds=True)
    sub = add("preview", cmd_preview, "Print the colors the device would show.", leds=True)
    sub.add_argument("--ms", type=int, default=2000)
    sub.add_argument("--step", type=int, default=250)
    add("write", cmd_write, "Validate and write LEDS.LED.", device=True)
    sub = add("read", cmd_read, "Print LEDS.LED, INIT.LED or STATUS.TXT from the device.",
              program=False, device=True)
    sub.add_argument("--file", default="LEDS.LED", choices=("LEDS.LED", "INIT.LED", "STATUS.TXT"))
    sub = add("watch", cmd_watch, "Write the configured program whenever it changes.",
              program=False, device=True)
    sub.add_argument("--poll", type=float, default=float(os.environ.get("POLL_SECONDS", "2")))
    sub.add_argument("--start-delay", type=float,
                     default=float(os.environ.get("START_DELAY_SECONDS", "2")),
                     help="Seconds the LEDs stay off at startup. Default: 2.")

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ProgramError, DeviceError, OSError, UnicodeDecodeError) as exc:
        print(f"sidepulse-hw: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
