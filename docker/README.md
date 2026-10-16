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

## TrueNAS app from the GUI

`.github/workflows/docker.yml` publishes the image to
`ghcr.io/<owner>/sidepulse-hw`. Make the package public once in GitHub
(Packages > sidepulse-hw > Package settings), then in TrueNAS open
Apps > Discover Apps > Custom App > Install via YAML and paste
`truenas-app.yaml` with your device path. To change the LEDs, edit
`SIDEPULSE_PROGRAM` from the app's Edit page; the app restarts and writes it.

## Tests

    docker build --target test -f docker/Dockerfile .

The workflow runs this before publishing, and does not publish on failure.
