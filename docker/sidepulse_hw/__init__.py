"""Validate SidePulse LED programs and write them to a device safely.

The validator runs the firmware's own LEDS.LED parser (sdled.wasm) in a
sandboxed WebAssembly runtime, so a program is accepted only if the device
would accept it. The writer updates LEDS.LED in place on the unmounted device and
can only ever touch that file.
"""
from .program import ProgramError, preview, validate
from .device import Device, DeviceError

__all__ = ["Device", "DeviceError", "ProgramError", "preview", "validate"]
