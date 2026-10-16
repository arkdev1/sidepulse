"""Write validated programs to a SidePulse FAT volume with mtools.

The volume is never mounted. Only LEDS.LED can be written: the target name is
a constant, not a parameter, so no caller can reach FIRMWARE.BIN or INIT.LED.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time

from .program import Program, ProgramError, validate

TARGET = "::LEDS.LED"
LABELS = {"PULSEDOT": 2, "SIDEPULSE": 8}
MIN_INTERVAL_SECONDS = 1.0
MTOOLS_ENV = {"MTOOLS_SKIP_CHECK": "1", "MTOOLS_NO_VFAT": "1"}


class DeviceError(RuntimeError):
    pass


def _mtools(*args: str) -> str:
    env = {**os.environ, **MTOOLS_ENV}
    try:
        result = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeviceError(f"{args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise DeviceError(f"{args[0]} failed: {detail}")
    return result.stdout


class Device:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        self.led_count = self._identify()
        self._lock = threading.Lock()
        self._last_text: str | None = None
        self._last_write = 0.0

    def _identify(self) -> int:
        output = _mtools("mlabel", "-s", "-i", self.path, "::")
        # "Volume label is PulseDot (abbr=PULSEDOT   )" on recent mtools.
        words = output.replace("Volume label is", "").split()
        label = words[0].upper() if words else ""
        if label not in LABELS:
            raise DeviceError(f"{self.path}: volume label {label!r} is not a SidePulse")
        _mtools("mdir", "-b", "-i", self.path, "::FIRMWARE.BIN")
        return LABELS[label]

    def read_program(self) -> str:
        return _mtools("mtype", "-i", self.path, TARGET)

    def write_program(self, text: str) -> bool:
        """Validate and write. Returns False when the device already has it."""

        program: Program = validate(text, self.led_count)
        with self._lock:
            if program.text == self._last_text:
                return False
            wait = self._last_write + MIN_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with tempfile.NamedTemporaryFile("w", encoding="ascii", suffix=".LED") as tmp:
                tmp.write(program.text)
                tmp.flush()
                _mtools("mcopy", "-o", "-i", self.path, tmp.name, TARGET)
            os.sync()
            self._last_text = program.text
            self._last_write = time.monotonic()
            return True


__all__ = ["Device", "DeviceError", "ProgramError"]
