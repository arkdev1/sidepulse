"""Run inside the image: docker run --rm --entrypoint python IMAGE -m unittest discover -s tests"""
import os
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from sidepulse_hw import Device, DeviceError, ProgramError, channel, preview, validate
from sidepulse_hw import device as device_module
from sidepulse_hw.__main__ import Watcher

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

    def test_rewrites_after_another_writer(self):
        path = make_image(self.tmp.name, "PulseDot")
        first, other = Device(path), Device(path)
        self.assertTrue(first.write_program("#00FF00"))
        self.assertTrue(other.write_program("#8000FF"))
        self.assertTrue(first.write_program("#00FF00"))
        self.assertEqual(read(path, "LEDS.LED"), b"#00FF00\n")

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


class ChannelTests(unittest.TestCase):
    def test_result_round_trip_and_age(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "LEDS.LED"
            self.assertIsNone(channel.read_result(program))
            self.assertIsNone(channel.age_seconds(program))
            channel.replace_file(program, "#00FF00\n")
            channel.touch(program, now=1000.0)
            self.assertAlmostEqual(channel.age_seconds(program, now=1300.0), 300.0, places=3)
            digest = channel.digest("#00FF00\n")
            channel.write_result(program, digest, "rejected", "line 1,  column 2:\n bad color")
            self.assertEqual(channel.read_result(program),
                             ("rejected", digest, "line 1, column 2: bad color"))
            self.assertEqual(channel.await_result(program, digest, 0.0)[0], "rejected")
            self.assertIsNone(channel.await_result(program, "0" * 16, 0.2))
            with self.assertRaises(ValueError):
                channel.write_result(program, digest, "maybe")
            self.assertEqual(sorted(os.listdir(directory)), ["LEDS.LED", "LEDS.LED.result"])


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.image = make_image(self.directory.name, "PULSEDOT")
        self.device = Device(self.image)
        # make_image leaves its source files in the directory; the channel
        # gets its own, like /dev/shm in the app.
        self.shm = Path(self.directory.name) / "shm"
        self.shm.mkdir()
        self.program = self.shm / "LEDS.LED"
        device_module.MIN_INTERVAL_SECONDS = 0.0

    def leds(self):
        return read(self.image, "LEDS.LED").strip()

    def tearDown(self):
        device_module.MIN_INTERVAL_SECONDS = 1.0
        self.directory.cleanup()

    def test_writes_changes_and_answers_each_attempt(self):
        watcher = Watcher(self.device, path=self.program)
        watcher.step()  # no file yet: nothing to do, nothing to answer
        self.assertIsNone(channel.read_result(self.program))

        channel.replace_file(self.program, "#00FF00\n")
        watcher.step()
        self.assertEqual(self.leds(), b"#00FF00")
        outcome, digest, detail = channel.read_result(self.program)
        self.assertEqual((outcome, digest), ("unconfirmed", channel.digest("#00FF00\n")))
        self.assertIn("leds_len", detail)  # the test image has no firmware status

        channel.touch(self.program)  # a heartbeat is not a change
        with patch.object(self.device, "write_program", side_effect=AssertionError("rewrote")):
            watcher.step()

        channel.replace_file(self.program, "#GGGGGG\n")
        watcher.step()
        self.assertEqual(self.leds(), b"#00FF00")
        self.assertEqual(channel.read_result(self.program)[:2],
                         ("rejected", channel.digest("#GGGGGG\n")))

    def test_stale_guard_takes_over_and_hands_back(self):
        watcher = Watcher(self.device, path=self.program, stale_after=300,
                          stale_program="#8000FF\n", now=1000.0)
        watcher.step(now=1100.0)  # missing, but not for long enough yet
        self.assertEqual(self.leds(), b"off")

        watcher.step(now=1301.0)  # nobody ever wrote it: the writer never came up
        self.assertEqual(self.leds(), b"#8000FF")
        self.assertTrue(watcher.showing_stale)

        channel.replace_file(self.program, "#00FF00\n")
        channel.touch(self.program, now=1400.0)
        watcher.step(now=1401.0)
        self.assertEqual(self.leds(), b"#00FF00")
        self.assertFalse(watcher.showing_stale)

        watcher.step(now=1700.0)  # 300s of silence: still fine
        self.assertEqual(self.leds(), b"#00FF00")
        watcher.step(now=1701.0)  # one more second: the writer is gone
        self.assertEqual(self.leds(), b"#8000FF")

        channel.touch(self.program, now=1800.0)  # back, same program as before
        watcher.step(now=1801.0)
        self.assertEqual(self.leds(), b"#00FF00")

    def test_inline_program_ignores_the_stale_guard(self):
        watcher = Watcher(self.device, inline="#0000FF\n", path=self.program,
                          stale_after=1, now=0.0)
        watcher.step(now=5000.0)
        self.assertEqual(self.leds(), b"#0000FF")
        self.assertEqual(os.listdir(self.shm), [])  # no file read, no answer written


if __name__ == "__main__":
    unittest.main()
