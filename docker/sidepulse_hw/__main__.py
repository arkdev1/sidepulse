"""sidepulse-hw: validate, preview, write and watch LED programs."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from . import channel
from .device import Device, DeviceError
from .program import ProgramError, normalize, preview, validate

DEFAULT_DEVICE = os.environ.get("SIDEPULSE_DEV", "/dev/sidepulse")
DEFAULT_PROGRAM_FILE = "/program/LEDS.LED"
# Shown when nobody has refreshed the program file for STALE_SECONDS: the
# writer died or never started, and a frozen "all good" would be a lie.
DEFAULT_STALE_PROGRAM = "#8000FF 2s pulse\noff 2s none\nrepeat"


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


MODELS = {"PULSEDOT": "SidePulse Dot", "SIDEPULSE": "SidePulse Pro"}


def describe(text: str) -> str:
    text = normalize(text)
    lines = [l for l in text.strip().splitlines() if not l.startswith("brightness ")] or [""]
    first = lines[0] if lines else ""
    more = f" (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
    level = next((l.split()[1] for l in text.splitlines() if l.startswith("brightness ")), None)
    return f"{len(text.encode())} bytes, {first!r}{more}" + (f", brightness {level}" if level else "")


def report_applied(device: Device, expected_len: int) -> tuple[str, str]:
    """Confirm from STATUS.TXT that the firmware picked up the write."""

    for _ in range(7):
        time.sleep(0.3)
        status = device.status()
        if "leds_len" not in status:
            log("WARN  STATUS.TXT has no leds_len; cannot confirm the firmware applied it")
            return "unconfirmed", "STATUS.TXT has no leds_len"
        applied = status["leds_len"] == str(expected_len)
        if applied:
            break
    detail = (f"leds_len={status.get('leds_len', '?')} leds_applies={status.get('leds_applies', '?')} "
              f"leds_event={status.get('leds_event', '?')}")
    if applied:
        log(f"INFO  firmware applied it ({detail})")
        return "applied", detail
    log(f"WARN  firmware has not applied it after 2s ({detail})")
    return "unconfirmed", f"not applied after 2s ({detail})"


def write_and_report(device: Device, text: str, what: str) -> tuple[str, str]:
    """Write unless the device already shows it; (outcome, detail) for the log and the channel."""

    if device.write_program(text):
        log(f"INFO  wrote {what}: {describe(text)}")
        return report_applied(device, len(normalize(text).encode()))
    log(f"INFO  {what} already on the device, nothing written")
    return "applied", "already on the device"


class Watcher:
    """The watch loop, one step at a time so it can be tested without sleeping.

    The program comes from SIDEPULSE_PROGRAM, or from a file another process
    replaces and touches (sidepulse_truenas, an editor). With stale_after set,
    a file nobody has refreshed for that long means the writer is gone, and
    the stale program takes over until it comes back. Every attempt is
    answered in the file's .result sibling, so the writer can log what the
    device did.
    """

    def __init__(self, device: Device, *, inline: str = "", path: Path | None = None,
                 stale_after: float = 0.0, stale_program: str = DEFAULT_STALE_PROGRAM,
                 now: float | None = None) -> None:
        self.device = device
        self.inline = inline if inline.strip() else ""
        self.path = path if path is not None else Path(DEFAULT_PROGRAM_FILE)
        self.stale_after = max(0.0, stale_after)
        self.stale_program = stale_program
        self.started = time.time() if now is None else now
        self.last_digest: str | None = None
        self.last_source: str | None = None
        self.showing_stale = False

    def program(self) -> tuple[str | None, str]:
        """The program to show and where it came from, for the log."""

        if self.inline:
            return self.inline, "SIDEPULSE_PROGRAM"
        try:
            return self.path.read_text(encoding="ascii"), str(self.path)
        except FileNotFoundError:
            return None, f"{self.path} (missing)"
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"{self.path} (unreadable: {exc})"

    def age(self, now: float | None = None) -> float:
        """Seconds since the file was last written or touched; since start if missing."""

        current = time.time() if now is None else now
        age = channel.age_seconds(self.path, current)
        return current - self.started if age is None else age

    def step(self, now: float | None = None) -> None:
        text, source = self.program()
        if source != self.last_source:
            log(f"INFO  program source: {source}")
            self.last_source = source
        if self.stale_after > 0 and not self.inline:
            age = self.age(now)
            if age > self.stale_after:
                if not self.showing_stale:
                    self.show_stale(age)
                return
            if self.showing_stale:
                log(f"INFO  {self.path} is being refreshed again; back to its program")
                self.showing_stale = False
                self.last_digest = None
        if text is None:
            return
        program_digest = channel.digest(text)
        if program_digest == self.last_digest:
            return
        self.apply(text, program_digest)

    def show_stale(self, age: float) -> None:
        log(f"WARN  {self.path} has not been refreshed for {age:.0f}s (limit {self.stale_after:g}s); "
            f"the writer is down, showing the stale program")
        self.showing_stale = True
        self.last_digest = None
        try:
            write_and_report(self.device, self.stale_program, "stale program")
        except (ProgramError, DeviceError) as exc:
            log(f"ERROR stale program failed: {exc}")

    def apply(self, text: str, program_digest: str) -> None:
        try:
            outcome, detail = write_and_report(self.device, text, "program")
            self.last_digest = program_digest
        except ProgramError as exc:
            log(f"ERROR program rejected, device unchanged: {exc}")
            outcome, detail = "rejected", str(exc)
            self.last_digest = program_digest
        except DeviceError as exc:
            log(f"ERROR write failed, retrying on the next poll: {exc}")
            outcome, detail = "write-failed", str(exc)
        if not self.inline:
            try:
                channel.write_result(self.path, program_digest, outcome, detail)
            except OSError:
                pass  # a read-only program directory: nobody is listening


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

    stale_program = os.environ.get("STALE_PROGRAM", "").strip() or DEFAULT_STALE_PROGRAM
    if args.stale_after > 0:
        try:
            validate(stale_program, device.led_count)
        except ProgramError as exc:
            log(f"ERROR STALE_PROGRAM is invalid: {exc}")
            return 1
    watcher = Watcher(
        device,
        inline=os.environ.get("SIDEPULSE_PROGRAM", ""),
        path=Path(os.environ.get("SIDEPULSE_PROGRAM_FILE", DEFAULT_PROGRAM_FILE)),
        stale_after=args.stale_after,
        stale_program=stale_program,
    )
    if watcher.stale_after > 0 and not watcher.inline:
        log(f"INFO  stale guard: {watcher.path} must be refreshed every {watcher.stale_after:g}s, "
            f"otherwise '{stale_program.splitlines()[0]}'")

    log(f"INFO  startup: LEDs off, then {args.start_delay:g}s pause")
    try:
        write_and_report(device, "off", "startup off")
    except DeviceError as exc:
        log(f"ERROR startup off failed: {exc}")
    time.sleep(args.start_delay)

    while True:
        watcher.step()
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
    sub.add_argument("--poll", type=float, default=float(os.environ.get("POLL_SECONDS", "1")))
    sub.add_argument("--start-delay", type=float,
                     default=float(os.environ.get("START_DELAY_SECONDS", "2")),
                     help="Seconds the LEDs stay off at startup. Default: 2.")
    sub.add_argument("--stale-after", type=float,
                     default=float(os.environ.get("STALE_SECONDS", "0")),
                     help="Show STALE_PROGRAM when the program file has not been written or "
                          "touched for this many seconds. Default: 0 (off).")

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
