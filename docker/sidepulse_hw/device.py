"""Write validated programs to a SidePulse device in place.

Ported from scripts/write_led_raw.py: the device is never mounted and nothing
is allocated. The existing LEDS.LED cluster gets the new program first, then
its directory entry gets the new size, each step synced before the next, the
order the firmware expects. Only the LEDS.LED data sector and the size field
of its directory entry are ever written: the target name is a constant, so no
caller can reach FIRMWARE.BIN or INIT.LED.
"""
from __future__ import annotations

import fcntl
import os
import stat
import struct
import threading
import time
from dataclasses import dataclass

from .program import Program, ProgramError, validate

SECTOR = 512
# SidePulse volumes are tiny; this refuses any real disk, pool members included.
MAX_DISK_BYTES = 300_000
BLKGETSIZE64 = 0x80081272  # Linux <linux/fs.h>: _IOR(0x12, 114, size_t)
LABELS = {"PULSEDOT": 2, "SIDEPULSE": 8}
LED_NAME = b"LEDS    LED"
READABLE = {"LEDS.LED": LED_NAME, "INIT.LED": b"INIT    LED", "STATUS.TXT": b"STATUS  TXT"}
MIN_INTERVAL_SECONDS = 1.0


class DeviceError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DeviceError(message)


@dataclass(frozen=True)
class Entry:
    name: bytes
    attributes: int
    cluster: int
    size: int
    sector_offset: int  # byte offset of the directory sector holding it
    entry_offset: int  # offset of the entry within that sector


@dataclass(frozen=True)
class Layout:
    data_start: int  # first data sector
    sectors_per_cluster: int
    clusters: int
    label: str
    entries: tuple[Entry, ...]
    fat: bytes

    def find(self, name: bytes) -> Entry | None:
        for entry in self.entries:
            if entry.name == name and not entry.attributes & 0x18:
                return entry
        return None

    def cluster_offset(self, cluster: int) -> int:
        return (self.data_start + (cluster - 2) * self.sectors_per_cluster) * SECTOR

    def next_cluster(self, cluster: int) -> int:
        packed = struct.unpack_from("<H", self.fat, cluster + cluster // 2)[0]
        return (packed >> (4 if cluster & 1 else 0)) & 0xFFF


def device_size(fd: int) -> int:
    mode = os.fstat(fd).st_mode
    if stat.S_ISBLK(mode):
        return struct.unpack("Q", fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0]
    require(stat.S_ISREG(mode), "Not a block device.")
    return os.fstat(fd).st_size  # disk images, for tests


def read_exact(fd: int, length: int, offset: int) -> bytes:
    data = os.pread(fd, length, offset)
    require(len(data) == length, "Short read; refusing to continue.")
    return data


def read_layout(fd: int, size: int) -> Layout:
    boot = read_exact(fd, SECTOR, 0)
    bps, spc, reserved, fats, root_entries, total = struct.unpack_from("<HBHBHH", boot, 11)
    fat_sectors = struct.unpack_from("<H", boot, 22)[0]
    hidden, total32 = struct.unpack_from("<II", boot, 28)
    total = total or total32
    require(boot[510:512] == b"\x55\xaa" and bps == SECTOR, "Not a FAT volume with 512-byte sectors.")
    require(fats in (1, 2) and hidden == 0 and reserved > 0, "Unexpected FAT layout.")
    require(root_entries > 0 and fat_sectors > 0, "Unexpected FAT layout.")
    require(spc in (1, 2, 4, 8, 16, 32, 64, 128), "Invalid cluster size.")
    root = reserved + fats * fat_sectors
    data_start = root + (root_entries * 32 + SECTOR - 1) // SECTOR
    clusters = (total - data_start) // spc
    require(0 < total * SECTOR <= size and 0 < clusters < 4085, "Not a FAT12 volume that fits the device.")
    fat = read_exact(fd, fat_sectors * SECTOR, reserved * SECTOR)
    require(((clusters + 2) * 3 + 1) // 2 <= len(fat), "FAT is too small for this volume.")

    label = boot[43:54].decode("ascii", "replace").strip() if boot[38] == 0x29 else ""
    entries = []
    for index in range(root_entries):
        sector_offset = (root + index // 16) * SECTOR
        if index % 16 == 0:
            directory = read_exact(fd, SECTOR, sector_offset)
        entry_offset = (index % 16) * 32
        raw = directory[entry_offset : entry_offset + 32]
        if raw[0] == 0:
            break
        if raw[0] == 0xE5 or raw[11] == 0x0F:  # deleted, long-name fragment
            continue
        if raw[11] & 0x08 and not raw[11] & 0x10:
            label = raw[:11].decode("ascii", "replace").strip()
            continue
        cluster = struct.unpack_from("<H", raw, 26)[0]
        file_size = struct.unpack_from("<I", raw, 28)[0]
        entries.append(Entry(raw[:11], raw[11], cluster, file_size, sector_offset, entry_offset))
    return Layout(data_start, spc, clusters, label.upper(), tuple(entries), fat)


class Device:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self._lock = threading.Lock()
        self._last_text: str | None = None
        self._last_write = 0.0
        with self._open(os.O_RDONLY) as (_, layout):
            require(layout.label in LABELS, f"{self.path}: volume label {layout.label!r} is not a SidePulse")
            require(layout.find(b"FIRMWAREBIN") is not None, f"{self.path}: no FIRMWARE.BIN, not a SidePulse")
            self._led_entry(layout)
            self.led_count = LABELS[layout.label]

    class _Opened:
        def __init__(self, device: "Device", flags: int) -> None:
            self.fd = os.open(device.path, flags | os.O_NOFOLLOW)

        def __enter__(self):
            try:
                self.size = device_size(self.fd)
                require(0 < self.size <= MAX_DISK_BYTES,
                        f"Refusing a {self.size:,}-byte disk; SidePulse volumes are under {MAX_DISK_BYTES:,}.")
                # Read what the device holds now, not what the host cached earlier.
                os.posix_fadvise(self.fd, 0, 0, os.POSIX_FADV_DONTNEED)
                return self.fd, read_layout(self.fd, self.size)
            except BaseException:
                os.close(self.fd)
                raise

        def __exit__(self, *exc) -> None:
            os.close(self.fd)

    def _open(self, flags: int) -> "_Opened":
        try:
            return Device._Opened(self, flags)
        except OSError as exc:
            raise DeviceError(f"{self.path}: {exc.strerror}") from exc

    @staticmethod
    def _led_entry(layout: Layout) -> Entry:
        entry = layout.find(LED_NAME)
        require(entry is not None, "Existing LEDS.LED not found in the root directory.")
        require(not entry.attributes & 0x01, "LEDS.LED is read-only.")
        require(2 <= entry.cluster < layout.clusters + 2, "Invalid LEDS.LED cluster.")
        following = layout.next_cluster(entry.cluster)
        require(following >= 0xFF8 or (2 <= following < layout.clusters + 2 and following != entry.cluster),
                "LEDS.LED cluster is unallocated or invalid.")
        return entry

    def read_file(self, name: str = "LEDS.LED") -> str:
        short = READABLE.get(name.upper())
        require(short is not None, f"Only {', '.join(READABLE)} can be read.")
        with self._open(os.O_RDONLY) as (fd, layout):
            entry = layout.find(short)
            require(entry is not None, f"{name} not found.")
            cluster_bytes = layout.sectors_per_cluster * SECTOR
            require(2 <= entry.cluster < layout.clusters + 2 and entry.size <= cluster_bytes,
                    f"{name} spans more than one cluster; not read.")
            return read_exact(fd, entry.size, layout.cluster_offset(entry.cluster)).decode("utf-8", "replace")

    def read_program(self) -> str:
        return self.read_file("LEDS.LED")

    def write_program(self, text: str) -> bool:
        """Validate and write. Returns False when the device already has it."""

        program: Program = validate(text, self.led_count)
        payload = program.text.encode("ascii")
        with self._lock:
            if program.text == self._last_text:
                return False
            wait = self._last_write + MIN_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with self._open(os.O_RDWR) as (fd, layout):
                entry = self._led_entry(layout)
                data_offset = layout.cluster_offset(entry.cluster)
                directory = bytearray(read_exact(fd, SECTOR, entry.sector_offset))
                require(directory[entry.entry_offset : entry.entry_offset + 11] == LED_NAME,
                        "Directory changed while writing; refusing.")
                struct.pack_into("<I", directory, entry.entry_offset + 28, len(payload))
                # Data first, file size last, each synced before the next.
                for offset, sector in ((data_offset, payload.ljust(SECTOR, b"\0")),
                                       (entry.sector_offset, bytes(directory))):
                    require(0 <= offset <= self._size_of(fd) - SECTOR and offset % SECTOR == 0,
                            "Write would be outside the device or unaligned.")
                    require(os.pwrite(fd, sector, offset) == SECTOR, "Short write.")
                    os.fsync(fd)
            self._last_text = program.text
            self._last_write = time.monotonic()
            return True

    @staticmethod
    def _size_of(fd: int) -> int:
        return device_size(fd)


__all__ = ["Device", "DeviceError", "ProgramError"]
