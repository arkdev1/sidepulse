"""Run inside the image: docker run --rm --entrypoint python IMAGE -m unittest discover -s tests"""
import os
import subprocess
import tempfile
import unittest

from sidepulse_hw import Device, DeviceError, ProgramError, preview, validate
from sidepulse_hw import device as device_module

ENV = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}


def make_image(directory, label, *, firmware=True):
    path = os.path.join(directory, f"{label}.img")
    with open(path, "wb") as image:
        image.truncate(1440 * 1024)
    subprocess.run(["mformat", "-i", path, "-v", label, "::"], env=ENV, check=True)
    files = {"LEDS.LED": b"off\n", "INIT.LED": b"#FFFFFF\n"}
    if firmware:
        files["FIRMWARE.BIN"] = b"\x5a" * 4096
    for name, data in files.items():
        source = os.path.join(directory, name)
        with open(source, "wb") as handle:
            handle.write(data)
        subprocess.run(["mcopy", "-i", path, source, f"::{name}"], env=ENV, check=True)
    return path


def read(path, name):
    return subprocess.run(
        ["mtype", "-i", path, f"::{name}"], env=ENV, check=True, capture_output=True
    ).stdout


class ValidateTests(unittest.TestCase):
    def test_accepts_valid_programs(self):
        self.assertEqual(validate("#FF00FF 1s pulse\r\nrepeat").text, "#FF00FF 1s pulse\nrepeat\n")
        validate("off", 2)

    def test_rejects_what_the_firmware_rejects(self):
        for text in ("on", "#GG0000", "brightness 999\n#FF0000"):
            with self.assertRaises(ProgramError, msg=text):
                validate(text)

    def test_rejects_oversized_and_non_ascii(self):
        for text in ("x" * 600, "#FF0000\n" * 21, "#FF0000 ★"):
            with self.assertRaises(ProgramError):
                validate(text)

    def test_preview_renders_frames(self):
        frames = preview("#FF0000", 2, duration_ms=0)
        self.assertEqual(frames[-1], [(255, 0, 0), (255, 0, 0)])


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        device_module.MIN_INTERVAL_SECONDS = 0

    def test_writes_only_leds_led(self):
        path = make_image(self.tmp.name, "PulseDot")
        firmware, init = read(path, "FIRMWARE.BIN"), read(path, "INIT.LED")
        device = Device(path)
        self.assertEqual(device.led_count, 2)
        self.assertTrue(device.write_program("#00FF00"))
        self.assertFalse(device.write_program("#00FF00"))
        self.assertEqual(read(path, "LEDS.LED"), b"#00FF00\n")
        self.assertEqual(read(path, "FIRMWARE.BIN"), firmware)
        self.assertEqual(read(path, "INIT.LED"), init)

    def test_invalid_program_leaves_device_unchanged(self):
        path = make_image(self.tmp.name, "SidePulse")
        with self.assertRaises(ProgramError):
            Device(path).write_program("on")
        self.assertEqual(read(path, "LEDS.LED"), b"off\n")

    def test_refuses_foreign_volumes(self):
        with self.assertRaises(DeviceError):
            Device(make_image(self.tmp.name, "DATA"))
        with self.assertRaises(DeviceError):
            Device(make_image(self.tmp.name, "PulseDot", firmware=False))


if __name__ == "__main__":
    unittest.main()
