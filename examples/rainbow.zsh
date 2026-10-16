# SidePulse Pro rainbow. Source this file from ~/.zshrc, then:
#   rainbow                  rainbow for 10 seconds, then fade out
#   rainbow --on             keep it running (touches the volume every minute
#                               so the MacBook SD reader does not power it off)
#   rainbow --off            turn the LEDs off and stop the keepalive
# Pass --device /Volumes/<name> to pick a device. The status-bar app overwrites
# the LEDs on the next agent state change; stop it to keep the rainbow.
rainbow() {
  local mode=once
  case "$1" in
    --on|--off) mode=${1#--}; shift ;;
  esac
  local palette='#ff0000 #ff8800 #ffff00 #00ff00 #00ccff #0044ff #8800ff #ff00cc\nroll 2s'
  local pidfile="${TMPDIR:-/tmp}/rainbow-keepalive.pid"

  # Stop any previous keepalive loop.
  if [[ -f $pidfile ]]; then
    kill "$(<"$pidfile")" 2>/dev/null
    rm -f "$pidfile"
  fi

  case $mode in
    off)
      sidepulse write 'off 500ms ease' "$@"
      ;;
    once)
      sidepulse write "$palette\nrepeat 5\noff 500ms ease" "$@"
      ;;
    on)
      sidepulse write "$palette\nrepeat" "$@" || return
      # The MacBook SD reader powers SidePulse Pro off after ~3 minutes idle.
      local volume= i
      for (( i = 1; i < $#; i++ )); do
        [[ ${@[i]} == --device ]] && volume=${@[i+1]%/LEDS.LED}
      done
      [[ -z $volume ]] && volume=(/Volumes/SidePulse*(N[1]))
      if [[ -d $volume ]]; then
        ( while touch "$volume/keepalive" 2>/dev/null; do sleep 60; done ) &!
        echo $! > "$pidfile"
        echo "rainbow: keepalive running on $volume (rainbow --off to stop it)"
      else
        echo "rainbow: SidePulse Pro volume not found, no keepalive (pass --device)" >&2
      fi
      ;;
  esac
}
