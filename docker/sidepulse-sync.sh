#!/bin/sh
# Copies the LED program to the device whenever it changes. The program comes
# from SIDEPULSE_PROGRAM (set in the TrueNAS app form) or, when that is empty,
# from /program/LEDS.LED. Only LEDS.LED is ever written; the device is checked
# to be a SidePulse volume first.
set -eu

DEVICE=${SIDEPULSE_DEV:-/dev/sidepulse}
PROGRAM=/program/LEDS.LED
POLL_SECONDS=${POLL_SECONDS:-2}
MAX_BYTES=512
MAX_LINES=20
export MTOOLS_SKIP_CHECK=1 MTOOLS_NO_VFAT=1

log() { echo "sidepulse: $*"; }

if [ -n "${SIDEPULSE_PROGRAM:-}" ]; then
    PROGRAM=/tmp/LEDS.LED
    printf '%s\n' "$SIDEPULSE_PROGRAM" > "$PROGRAM"
fi

check_device() {
    label=$(mlabel -s -i "$DEVICE" :: 2>/dev/null || true)
    case "$label" in
        *PULSEDOT*|*PulseDot*|*SIDEPULSE*|*SidePulse*) ;;
        *) log "refusing $DEVICE: volume label '$label' is not a SidePulse"; return 1 ;;
    esac
    if ! mdir -b -i "$DEVICE" ::FIRMWARE.BIN >/dev/null 2>&1; then
        log "refusing $DEVICE: no FIRMWARE.BIN, not a SidePulse volume"
        return 1
    fi
}

valid_program() {
    bytes=$(wc -c < "$PROGRAM")
    lines=$(wc -l < "$PROGRAM")
    if [ "$bytes" -gt "$MAX_BYTES" ] || [ "$lines" -gt "$MAX_LINES" ]; then
        log "program too large ($bytes bytes, $lines lines; max $MAX_BYTES / $MAX_LINES)"
        return 1
    fi
}

check_device || exit 1
log "device ok, watching $PROGRAM"

last=""
while :; do
    if [ -f "$PROGRAM" ]; then
        current=$(md5sum < "$PROGRAM")
        if [ "$current" != "$last" ] && valid_program; then
            if mcopy -o -i "$DEVICE" "$PROGRAM" ::LEDS.LED; then
                sync
                log "wrote LEDS.LED"
                last=$current
            else
                log "write failed, retrying"
            fi
        fi
    fi
    sleep "$POLL_SECONDS"
done
