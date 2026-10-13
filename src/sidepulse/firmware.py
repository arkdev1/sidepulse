from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .device_writer import discover_devices, sync_directory
from .security import NetworkDisabledError, open_url


RELEASES_URL = "https://api.github.com/repos/inteliwear/sidepulse/contents/firmware?ref=main"
DOWNLOAD_ROOT = "https://raw.githubusercontent.com/inteliwear/sidepulse/main/firmware"
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
PRODUCT_NAMES = {"dot": "SidePulse Dot", "pro": "SidePulse Pro"}
PACKAGE_FILES = {"FIRMWARE.BIN", "README.txt", "RELEASE_NOTES.txt", "SHA256SUMS.txt"}

# OTA ZIPs reviewed in this repository (firmware/v*/SHA256SUMS.txt). A
# package is only installed when its SHA-256 is listed here, so neither a
# compromised download source nor a swapped local file can flash a device.
# tests/test_firmware.py keeps this table in sync with the repository.
PINNED_PACKAGE_SHA256 = {
    "sidepulse-dot-1.1.0-ota.zip": "0e7325cacfa08a30cced5d0250ab3032e4c4b2b615dc3a09acb99f6021627761",
    "sidepulse-pro-1.1.0-ota.zip": "1efc35498f837932938aaf1b3e4f2889549d09cfbf974ae1132cafb7768b6fac",
    "sidepulse-dot-1.1.14-ota.zip": "1d82c907dfb945f8714e9021afaf793a161eff13182967bcc5d63ebbc212cdb7",
}


# The pinned ZIPs ship inside the installed package (resources/firmware holds
# copies of the repository's firmware/v*/*.zip, checked by
# tests/test_firmware.py), so upgrades work offline.
BUNDLED_FIRMWARE = "firmware"


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class FirmwareDevice:
    root: Path
    product: str
    version: str
    serial: str

    @property
    def name(self) -> str:
        return PRODUCT_NAMES[self.product]


@dataclass(frozen=True)
class FirmwarePackage:
    product: str
    version: str
    payload: bytes
    source: str = ""
    sha256: str = ""


def normalize_version(value: str) -> str:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise FirmwareError(f"Invalid firmware version: {value!r}. Use a version such as 1.1.0.")
    return ".".join(str(int(part or 0)) for part in match.groups())


def version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in normalize_version(value).split("."))


def read_device(root: Path) -> FirmwareDevice:
    root = root.expanduser().resolve()
    try:
        with (root / "STATUS.TXT").open("rb") as stream:
            text = stream.read(65536).decode("utf-8", errors="replace")
    except OSError as exc:
        raise FirmwareError(f"Cannot read {root / 'STATUS.TXT'}: {exc}") from exc
    fields = {}
    for line in text.replace("\x00", "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            fields[parts[0]] = parts[1]
    dot = "app_version" in fields or ("app_build" in fields and "fw_state" in fields)
    pro = "release_version" in fields or ("firmware_version" in fields and "firmware_slot" in fields)
    if dot == pro:
        raise FirmwareError(f"Cannot identify a SidePulse Dot or Pro from {root / 'STATUS.TXT'}.")
    product = "dot" if dot else "pro"
    version = fields.get("app_version" if dot else "release_version", "unknown")
    return FirmwareDevice(root, product, version, fields.get("serial", "unassigned"))


def find_devices(device_path: Path | None) -> list[FirmwareDevice]:
    if device_path is not None:
        return [read_device(device_path)]
    devices = []
    for candidate in discover_devices(file_name="STATUS.TXT"):
        try:
            devices.append(read_device(candidate.root))
        except FirmwareError:
            continue
    if not devices:
        raise FirmwareError("No mounted SidePulse device found. Connect it or pass --device /path/to/drive.")
    return devices


def download(url: str, *, limit: int = MAX_PACKAGE_BYTES) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sidepulse-cli", "Cache-Control": "no-cache"})
    try:
        with open_url(request, timeout=30) as response:
            data = response.read(limit + 1)
    except NetworkDisabledError as exc:
        raise FirmwareError(f"Firmware download blocked: {exc} Bundled packages and --file work offline.") from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise FirmwareError(f"Could not download firmware release data: {exc}. You can use --file with a local OTA ZIP.") from exc
    if len(data) > limit:
        raise FirmwareError("Firmware download exceeds the size limit.")
    return data


def published_versions() -> list[str]:
    try:
        entries = json.loads(download(RELEASES_URL, limit=1024 * 1024))
    except (ValueError, UnicodeError) as exc:
        raise FirmwareError("The firmware release listing is invalid.") from exc
    if not isinstance(entries, list):
        raise FirmwareError("The firmware release listing is invalid.")
    versions = [
        entry["name"][1:]
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "dir"
        and re.fullmatch(r"v\d+\.\d+\.\d+", str(entry.get("name", "")))
    ]
    if not versions:
        raise FirmwareError("No published firmware releases found.")
    return sorted(set(versions), key=version_key, reverse=True)


def parse_checksums(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise FirmwareError("Invalid firmware checksums.") from exc
    result = {}
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in result:
            raise FirmwareError("Invalid firmware checksums.")
        result[match[2]] = match[1].lower()
    return result


def verify_checksum(data: bytes, expected: str | None, name: str) -> None:
    if expected is None or hashlib.sha256(data).hexdigest() != expected:
        raise FirmwareError(f"Firmware checksum mismatch: {name}.")


def read_package(data: bytes, filename: str) -> FirmwarePackage:
    match = re.fullmatch(r"sidepulse-(dot|pro)-(\d+\.\d+\.\d+)-ota\.zip", filename)
    if not match:
        raise FirmwareError("Use a customer OTA ZIP named sidepulse-dot-VERSION-ota.zip or sidepulse-pro-VERSION-ota.zip.")
    product, version = match.groups()
    if len(data) > MAX_PACKAGE_BYTES:
        raise FirmwareError("Firmware package exceeds the size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            prefix = "" if set(names) == PACKAGE_FILES else filename[:-4] + "/"
            if len(names) != 4 or set(names) != {prefix + name for name in PACKAGE_FILES}:
                raise FirmwareError("Unexpected files in firmware package.")
            if sum(info.file_size for info in archive.infolist()) > MAX_PACKAGE_BYTES:
                raise FirmwareError("Expanded firmware package exceeds the size limit.")
            contents = {name: archive.read(prefix + name) for name in PACKAGE_FILES}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, zlib.error) as exc:
        raise FirmwareError(f"Invalid firmware ZIP: {exc}") from exc
    checksums = parse_checksums(contents["SHA256SUMS.txt"])
    if set(checksums) != PACKAGE_FILES - {"SHA256SUMS.txt"}:
        raise FirmwareError("Firmware package checksums are incomplete.")
    for name, digest in checksums.items():
        verify_checksum(contents[name], digest, name)
    title = f"{PRODUCT_NAMES[product]} firmware update - version {version}"
    if contents["README.txt"].splitlines()[:1] != [title.encode()]:
        raise FirmwareError("Firmware package model or version does not match its filename.")
    if not contents["FIRMWARE.BIN"]:
        raise FirmwareError("Firmware image is empty.")
    return FirmwarePackage(product, version, contents["FIRMWARE.BIN"])


def package_is_pinned_digest(digest: str) -> bool:
    return digest in PINNED_PACKAGE_SHA256.values()


def package_is_pinned(data: bytes) -> bool:
    return package_is_pinned_digest(hashlib.sha256(data).hexdigest())


def require_pinned(data: bytes, name: str, allow_unpinned: bool) -> None:
    if allow_unpinned or package_is_pinned(data):
        return
    raise FirmwareError(
        f"{name} is not a firmware package reviewed in this SidePulse build "
        f"(SHA-256 {hashlib.sha256(data).hexdigest()}). Pass --allow-unpinned to install it anyway."
    )


def bundled_packages(product: str) -> dict[str, object]:
    """Reviewed OTA ZIPs shipped with this build, by version."""
    root = files("sidepulse.resources") / BUNDLED_FIRMWARE
    found = {}
    pattern = re.compile(rf"sidepulse-{product}-(\d+\.\d+\.\d+)-ota\.zip")
    try:
        releases = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return found
    for release in releases:
        for entry in release.iterdir():
            match = pattern.fullmatch(entry.name)
            if match:
                found[match[1]] = entry
    return found


def checked_package(data: bytes, name: str, source: str, allow_unpinned: bool) -> FirmwarePackage:
    require_pinned(data, name, allow_unpinned)
    package = read_package(data, name)
    return FirmwarePackage(
        package.product,
        package.version,
        package.payload,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def load_package(
    product: str,
    version: str | None,
    path: Path | None,
    *,
    allow_unpinned: bool = False,
    online: bool = False,
) -> FirmwarePackage:
    if path is not None:
        path = path.expanduser()
        with path.open("rb") as stream:
            data = stream.read(MAX_PACKAGE_BYTES + 1)
        return checked_package(data, path.name, f"local file {path}", allow_unpinned)
    if not online:
        bundled = bundled_packages(product)
        if version:
            release = normalize_version(version)
            if release not in bundled:
                available = ", ".join(sorted(bundled, key=version_key)) or "none"
                raise FirmwareError(
                    f"Firmware {release} for {PRODUCT_NAMES[product]} is not bundled with this build "
                    f"(bundled: {available}). Use --file or --online."
                )
        elif bundled:
            release = max(bundled, key=version_key)
        else:
            raise FirmwareError(f"No {PRODUCT_NAMES[product]} firmware is bundled with this build. Use --file or --online.")
        entry = bundled[release]
        data = entry.read_bytes()
        return checked_package(data, entry.name, "bundled with this SidePulse build", allow_unpinned)
    releases = [normalize_version(version)] if version else published_versions()
    for release in releases:
        name = f"sidepulse-{product}-{release}-ota.zip"
        base = f"{DOWNLOAD_ROOT}/v{release}"
        checksums = parse_checksums(download(f"{base}/SHA256SUMS.txt", limit=65536))
        if name not in checksums:
            continue
        data = download(f"{base}/{name}")
        verify_checksum(data, checksums[name], name)
        return checked_package(data, name, f"downloaded from {base}", allow_unpinned)
    requested = f" for {normalize_version(version)}" if version else ""
    raise FirmwareError(f"No published {PRODUCT_NAMES[product]} firmware package found{requested}.")


def write_firmware(device: FirmwareDevice, package: FirmwarePackage) -> None:
    # Recheck identity after downloading, before opening the update file.
    if read_device(device.root) != device:
        raise FirmwareError("The connected device changed. Run the upgrade again.")
    target = device.root / "FIRMWARE.BIN"
    if target.is_symlink():
        raise FirmwareError("The firmware target must not be a symbolic link.")
    try:
        with target.open("wb") as stream:
            stream.write(package.payload)
            stream.flush()
            os.fsync(stream.fileno())
        sync_directory(device.root)
    except OSError as exc:
        raise FirmwareError(
            f"Firmware transfer did not finish: {exc}. Keep the device connected for at least "
            "10 seconds, then reconnect and check its version before retrying. "
            "Copy-based upgrades require a writable drive."
        ) from exc


def cmd_firmware_version(args: argparse.Namespace) -> int:
    try:
        devices = find_devices(args.device)
    except (FirmwareError, OSError) as exc:
        print(f"SidePulse firmware: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([
            {"model": device.name, "version": device.version, "serial": device.serial, "device": str(device.root)}
            for device in devices
        ]))
    else:
        for device in devices:
            print(f"{device.name}: {device.version}  ({device.root}, serial {device.serial})")
    return 0


def print_upgrade_warning(device: FirmwareDevice, package: FirmwarePackage) -> None:
    pinned = "yes" if package_is_pinned_digest(package.sha256) else "NO (--allow-unpinned)"
    lines = [
        "",
        "WARNING: firmware upgrade",
        f"  Device:   {device.name} at {device.root}, serial {device.serial}",
        f"  Upgrade:  {device.version} -> {package.version}",
        f"  Source:   {package.source}",
        f"  SHA-256:  {package.sha256}  (pinned: {pinned})",
        "",
        "  - The firmware image is encrypted and cannot be audited. The checksum only",
        "    proves these are the bytes reviewed in this repository, not what they do.",
        "  - The CLI cannot roll back (downgrades are refused). An interrupted transfer",
        "    can leave the device unusable: keep it connected for at least 10 seconds.",
        "  - Afterwards run `sidepulse firmware version`. For the Dot, also check that",
        "    its UsbDeviceSignature still ends in 080650 (a single mass-storage",
        "    interface, no keyboard/HID):",
        "    ioreg -r -c IOUSBHostDevice -l -w0 | grep -E 'USB Product Name|UsbDeviceSignature'",
        "",
    ]
    print("\n".join(lines), flush=True)


def confirm_upgrade(package: FirmwarePackage) -> bool:
    """Ask the user to type the target version; never assume consent without a terminal."""
    if not sys.stdin.isatty():
        print(
            "SidePulse firmware upgrade needs confirmation: run it in a terminal, or pass --yes.",
            file=sys.stderr,
        )
        return False
    try:
        answer = input(f"Type the target version ({package.version}) to install it, anything else cancels: ")
    except EOFError:
        return False
    return answer.strip() == package.version


def cmd_firmware_upgrade(args: argparse.Namespace) -> int:
    try:
        devices = find_devices(args.device)
        if len(devices) != 1:
            paths = "\n".join(f"  {device.root}" for device in devices)
            raise FirmwareError("Multiple SidePulse devices found. Select one with --device:\n" + paths)
        device = devices[0]
        print(f"{device.name}: firmware {device.version} ({device.root})", flush=True)
        package = load_package(
            device.product,
            args.release_version,
            args.file,
            allow_unpinned=args.allow_unpinned,
            online=args.online,
        )
        if package.product != device.product:
            raise FirmwareError(f"This ZIP is for {PRODUCT_NAMES[package.product]}, but the device is {device.name}.")
        try:
            current = version_key(device.version)
        except FirmwareError:
            current = None
        target = version_key(package.version)
        if current == target:
            print(f"{device.name} is already on firmware {package.version}.")
            return 0
        if current is not None and current > target:
            raise FirmwareError(f"Firmware {package.version} is older than installed version {device.version}; downgrade refused.")
        print_upgrade_warning(device, package)
        if args.dry_run:
            print(f"Would upgrade {device.name} from {device.version} to {package.version}. Package verified; no files written.")
            return 0
        if not args.yes and not confirm_upgrade(package):
            print("Firmware upgrade cancelled; nothing was written.", file=sys.stderr)
            return 1
        print(f"Sending firmware {package.version}. Keep the device connected.", flush=True)
        write_firmware(device, package)
        print(
            f"Firmware {package.version} transferred. Leave the device connected for at least 10 seconds "
            "while it applies the update, then reconnect and run `sidepulse firmware version` "
            "to confirm installation."
        )
        return 0
    except (FirmwareError, OSError) as exc:
        print(f"SidePulse firmware upgrade failed: {exc}", file=sys.stderr)
        return 1


def add_firmware_parser(subparsers: argparse._SubParsersAction) -> None:
    firmware = subparsers.add_parser("firmware", help="Show or upgrade device firmware.")
    commands = firmware.add_subparsers(dest="firmware_command", required=True)
    version = commands.add_parser("version", help="Show firmware versions of connected devices.")
    version.add_argument("--device", type=Path, help="Mounted SidePulse drive. Default: all detected devices.")
    version.add_argument("--json", action="store_true", help="Print device versions as JSON.")
    version.set_defaults(func=cmd_firmware_version)
    upgrade = commands.add_parser(
        "upgrade",
        help="Install the newest firmware bundled with this build (reviewed and pinned) on a connected device.",
    )
    upgrade.add_argument("--device", type=Path, help="Mounted SidePulse drive. Required when multiple devices are connected.")
    source = upgrade.add_mutually_exclusive_group()
    upgrade.add_argument(
        "--online",
        action="store_true",
        help="Look up releases on GitHub instead of the bundled packages (needs network access).",
    )
    source.add_argument("--version", dest="release_version", help="Install a specific release, such as 1.1.0.")
    source.add_argument("--file", type=Path, help="Install a local customer OTA ZIP.")
    upgrade.add_argument("--dry-run", action="store_true", help="Verify the package and show the warning without writing to the device.")
    upgrade.add_argument("--yes", action="store_true", help="Skip the typed confirmation (for scripts).")
    upgrade.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="Install a package whose SHA-256 is not pinned in this SidePulse build.",
    )
    upgrade.set_defaults(func=cmd_firmware_upgrade)
