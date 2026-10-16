"""sidepulse-hw: validate, preview, write and watch LED programs."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

from .device import Device, DeviceError
from .program import ProgramError, preview, validate

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


def watched_program() -> str | None:
    inline = os.environ.get("SIDEPULSE_PROGRAM", "")
    if inline.strip():
        return inline
    path = Path(os.environ.get("SIDEPULSE_PROGRAM_FILE", "/program/LEDS.LED"))
    try:
        return path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None


def cmd_watch(args) -> int:
    device = Device(args.device)
    log(f"device ok ({device.led_count} LEDs), watching for program changes")
    last = None
    while True:
        text = watched_program()
        digest = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        if text is not None and digest != last:
            try:
                if device.write_program(text):
                    log("wrote LEDS.LED")
                last = digest
            except ProgramError as exc:
                log(f"program rejected, device unchanged: {exc}")
                last = digest
            except DeviceError as exc:
                log(f"write failed, retrying: {exc}")
        time.sleep(args.poll)


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
