"""LED program validation with the firmware parser compiled to WebAssembly."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import wasmtime

MAX_BYTES = 512
MAX_LINES = 20
WASM_PATH = Path(os.environ.get("SIDEPULSE_WASM", Path(__file__).with_name("sdled.wasm")))
ERROR_NAMES = (
    "ok",
    "null-input",
    "too-long",
    "too-many-lines",
    "too-many-animation-lines",
    "syntax",
    "bad-color",
    "bad-index",
    "bad-time",
    "bad-brightness",
    "bad-repeat",
    "trailing-input",
)


class ProgramError(ValueError):
    def __init__(self, message: str, *, line: int = 0, column: int = 0) -> None:
        super().__init__(message)
        self.line = line
        self.column = column


@dataclass(frozen=True)
class Program:
    text: str
    led_count: int


class _Engine:
    """One sandboxed instance of sdled.wasm. The module has no imports, so it
    can reach nothing outside its own memory."""

    def __init__(self) -> None:
        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, str(WASM_PATH))
        if list(module.imports):
            raise RuntimeError("sdled.wasm unexpectedly imports host functions")
        self.store = wasmtime.Store(engine)
        instance = wasmtime.Instance(self.store, module, [])
        exports = instance.exports(self.store)
        self.memory = exports["memory"]
        self.reset_fn = exports["sdled_reset"]
        self.parse_fn = exports["sdled_parse"]
        self.step_fn = exports["sdled_step"]
        self.input_ptr = exports["sdled_input_ptr"](self.store)
        self.output_ptr = exports["sdled_output_ptr"](self.store)

    def parse(self, led_count: int, data: bytes, now_ms: int = 0) -> tuple[int, int, int]:
        self.reset_fn(self.store, led_count, now_ms)
        self.memory.write(self.store, data.ljust(MAX_BYTES, b"\0"), self.input_ptr)
        packed = self.parse_fn(self.store, led_count, len(data), now_ms)
        ok = packed & 1
        error = (packed >> 8) & 0xFF
        if ok:
            return 0, 0, 0
        return error or 5, (packed >> 16) & 0xFF, (packed >> 24) & 0xFF

    def step(self, led_count: int, now_ms: int) -> list[tuple[int, int, int]]:
        self.step_fn(self.store, led_count, now_ms)
        raw = self.memory.read(self.store, self.output_ptr, self.output_ptr + led_count * 3)
        return [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]


_engine: _Engine | None = None


def _get_engine() -> _Engine:
    global _engine
    if _engine is None:
        _engine = _Engine()
    return _engine


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text if text.endswith("\n") else text + "\n"


def validate(text: str, led_count: int = 8) -> Program:
    if led_count not in (2, 8):
        raise ProgramError("led_count must be 2 (Dot) or 8 (Pro).")
    text = normalize(text)
    if not text.strip():
        raise ProgramError("LED program is empty.")
    for number, line in enumerate(text.splitlines(), 1):
        for column, char in enumerate(line, 1):
            if not (char == "\t" or " " <= char <= "~"):
                raise ProgramError(
                    f"line {number}, column {column}: only printable ASCII is allowed",
                    line=number,
                    column=column,
                )
    data = text.encode("ascii")
    if len(data) > MAX_BYTES:
        raise ProgramError(f"LED program is {len(data)} bytes; max is {MAX_BYTES}.")
    lines = len(text.splitlines())
    if lines > MAX_LINES:
        raise ProgramError(f"LED program has {lines} lines; max is {MAX_LINES}.")
    error, line, column = _get_engine().parse(led_count, data)
    if error:
        name = ERROR_NAMES[error] if error < len(ERROR_NAMES) else f"error-{error}"
        raise ProgramError(
            f"line {line}, column {column}: {name}", line=line, column=column
        )
    return Program(text=text, led_count=led_count)


def preview(
    text: str, led_count: int = 8, *, duration_ms: int = 2000, step_ms: int = 100
) -> list[list[tuple[int, int, int]]]:
    """Frames the device would show, for a dry run before writing."""

    program = validate(text, led_count)
    engine = _get_engine()
    engine.parse(program.led_count, program.text.encode("ascii"), 0)
    return [
        engine.step(program.led_count, now)
        for now in range(0, max(duration_ms, 0) + 1, max(step_ms, 1))
    ]
