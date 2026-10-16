# SidePulse from a container (TrueNAS, any Linux host)

The container writes `LEDS.LED` straight into the device's FAT volume with
mtools. The host never mounts the device, and the container has no network,
no capabilities, a read-only root and access to that one block device only.

1. Unmount the device if you mounted it by hand: `sudo umount /mnt/SidePulse`.
   Writing while the host also has it mounted corrupts the volume.
2. Find its stable name, which survives replugs (unlike `/dev/sdc`):
   `ls -l /dev/disk/by-id/ | grep -v part`
3. `cp .env.example .env` and set `SIDEPULSE_DEVICE` to that path.
4. `sudo docker compose up -d --build`, then `sudo docker compose logs -f`.

Edit `program/LEDS.LED` (see LEDS_FORMAT.md, max 512 bytes / 20 lines) and
the container copies it to the device within `POLL_SECONDS`. It refuses to
start unless the volume label is a SidePulse one and `FIRMWARE.BIN` exists,
and it never writes any file other than `LEDS.LED`.
