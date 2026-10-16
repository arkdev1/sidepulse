"""Run inside the image: docker run --rm --entrypoint python IMAGE -m unittest discover -s tests"""
import os
import subprocess
import tempfile
import unittest

from sidepulse_hw import Device, DeviceError, ProgramError, preview, validate
from sidepulse_hw import device as device_module

ENV = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}


def make_image(directory, label, *, firmware=True, size=256 * 1024):
    """A FAT12 volume shaped like a SidePulse: one FAT, a tiny root directory."""
    path = os.path.join(directory, f"{label}.img")
    with open(path, "wb") as image:
        image.truncate(size)
    subprocess.run(
        ["mformat", "-i", path, "-T", str(size // 512), "-h", "1", "-s", "32",
         "-d", "1", "-r", "1", "-c", "4", "-v", label, "::"],
        env=ENV, check=True,
    )
    files = {"STATUS.TXT": b"fw 1.1.0\n" + b"x" * 3000 + b"\nserial SPD-000340\nend\n", "LEDS.LED": b"off\n", "INIT.LED": b"#FFFFFF\n"}
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
        with open(path, "rb") as image:
            before = image.read()
        device = Device(path)
        self.assertEqual(device.led_count, 2)
        self.assertTrue(device.write_program("#00FF00"))
        self.assertFalse(device.write_program("#00FF00"))
        self.assertEqual(read(path, "LEDS.LED"), b"#00FF00\n")
        self.assertEqual(device.read_program(), "#00FF00\n")
        with open(path, "rb") as image:
            after = image.read()
        changed = {i // 512 for i in range(len(before)) if before[i] != after[i]}
        entry = Device(path)
        # Exactly two sectors: LEDS.LED's data and its directory entry's size.
        self.assertEqual(len(changed), 2)
        root_dir = [i for i in changed if after[i * 512 : i * 512 + 512].find(b"LEDS    LED") >= 0]
        self.assertEqual(len(root_dir), 1)
        self.assertEqual(read(path, "INIT.LED"), b"#FFFFFF\n")
        self.assertEqual(read(path, "FIRMWARE.BIN"), b"\x5a" * 4096)
        # STATUS.TXT spans several clusters, like the real one.
        status = entry.read_file("STATUS.TXT")
        self.assertTrue(status.startswith("fw 1.1.0\n") and status.endswith("\nend\n"))
        self.assertEqual(status.encode(), read(path, "STATUS.TXT"))
        self.assertEqual(entry.status()["serial"], "SPD-000340")
        subprocess.run(["mtools", "-c", "mdir", "-i", path, "::"], env=ENV, check=True,
                       capture_output=True)

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

    def test_refuses_large_disks(self):
        big = os.path.join(self.tmp.name, "big")
        os.mkdir(big)
        with self.assertRaises(DeviceError):
            Device(make_image(big, "PulseDot", size=1440 * 1024))

    def test_reads_only_known_files(self):
        device = Device(make_image(self.tmp.name, "SidePulse"))
        with self.assertRaises(DeviceError):
            device.read_file("FIRMWARE.BIN")


if __name__ == "__main__":
    unittest.main()
