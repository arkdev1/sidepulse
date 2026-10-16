# sidepulse-hw: SidePulse from a container (TrueNAS, any Linux host)

`sidepulse_hw` is a small Python tool and SDK that validates LED programs and
writes them to a SidePulse Dot or Pro. It is the only thing that touches the
hardware, so anything built on top of it (a script, a web page) cannot write
a program the firmware would reject, or any file other than `LEDS.LED`.

- **Validation** runs the firmware's own parser (`sdled.wasm`, shared with
  the main package) in a sandboxed WebAssembly runtime, plus the 512 byte /
  20 line limits and a printable-ASCII check. Errors report line and column.
- **Writing** edits the FAT volume directly with mtools, so the host never
  mounts it. The target is fixed to `LEDS.LED`; `FIRMWARE.BIN` and
  `INIT.LED` are unreachable. The device must have a `PulseDot` or
  `SidePulse` label and a `FIRMWARE.BIN`, identical programs are skipped and
  writes are at most one per second.
- **Container**: no network, no capabilities, read-only root, access to that
  one block device only. The image is distroless (no shell, no package
  manager): Python, the stripped wasmtime library and the mtools binary,
  about 26 MB compressed. Base images are pinned by digest, and the build
  context is limited to the files in `Dockerfile.dockerignore`.

## Commands

    python -m sidepulse_hw validate [--leds 2|8] FILE|-
    python -m sidepulse_hw preview  [--leds 2|8] FILE|-    # colors over time
    python -m sidepulse_hw write    [--device DEV] FILE|-
    python -m sidepulse_hw read     [--device DEV]
    python -m sidepulse_hw watch    [--device DEV]         # container default

`watch` writes `SIDEPULSE_PROGRAM` when set, otherwise
`/program/LEDS.LED`, whenever it changes; rejected programs are logged and
the device is left as it was. From code:

    from sidepulse_hw import Device, validate, preview
    Device("/dev/sidepulse").write_program("#FF00FF 1s pulse\nrepeat")

## With docker compose

1. Unmount the device if you mounted it by hand: `sudo umount /mnt/SidePulse`.
   Writing while the host also has it mounted corrupts the volume.
2. Find its stable name, which survives replugs (unlike `/dev/sdc`):
   `ls -l /dev/disk/by-id/ | grep -v part`
3. `cp .env.example .env` and set `SIDEPULSE_DEVICE` to that path.
4. `sudo docker compose up -d --build`, then `sudo docker compose logs -f`.
5. Edit `program/LEDS.LED` (see LEDS_FORMAT.md).

## TrueNAS collector

`sidepulse_truenas` runs from the same image as a second service (see
`truenas-app.yaml`). It calls read-only TrueNAS API methods over `wss://`
(JSON-RPC 2.0, TrueNAS 24.10+) and writes the program for the highest-priority
state to the shared `/program/LEDS.LED`. It has network access and no device;
`sidepulse_hw` has the device and no network.

| State | Source | Default program |
|---|---|---|
| night | `NIGHT` window, default 23:00-07:00 | off |
| critical | unhealthy pool, alert ERROR or above | red blink |
| unreachable | API down for 3 polls, login or TLS failure | violet slow pulse |
| activity | running job in `ACTIVITY_JOBS` (replication, cloud sync, cloud backup, rsync, scrub) | blue pulse |
| update | `update.status` (or `update.check_available`), apps with `upgrade_available` | amber pulse |
| ok | none of the above | green |

Settings: `TRUENAS_HOST`, `TRUENAS_USERNAME`, `TRUENAS_API_KEY`,
`TRUENAS_CERT_SHA256` (SHA-256 of the NAS certificate; the log prints it when
it is missing or wrong, and nothing is sent to a server that does not match),
`TZ`, `NIGHT` (empty disables it), `NIGHT_SHOWS_CRITICAL`, `BRIGHTNESS`
(0-255, scales colors), `POLL_SECONDS` (30), `UPDATE_CHECK_MINUTES` (60),
`ACTIVITY_JOBS`, and `PROGRAM_<STATE>` to replace any program. Programs are
validated with the firmware parser at startup.

## Tests

    docker build --target test -f docker/Dockerfile .

The workflow runs this before publishing, and does not publish on failure.
