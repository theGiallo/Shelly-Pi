#!/bin/sh
# Applies a "set the clock" request made from the web page.
#
# The web app runs in a container and cannot change the host's clock, so it
# drops a Unix timestamp into /var/lib/shelly-pi/bridge/settime.req.
# shelly-settime.path notices the file and starts this script as root.
set -eu

REQ=${SHELLY_SETTIME_REQ:-/var/lib/shelly-pi/bridge/settime.req}
MIN=1767225600     # 2026-01-01, same lower bound as the app
MAX=4102444800     # 2100-01-01

[ -f "$REQ" ] || exit 0
# Only digits, dots and spaces are read. Delete the request first so a failure can't loop.
# The request is "<seconds, maybe with a fraction> <uptime when it was written>".
LINE=$(head -c 64 "$REQ" | tr -cd '0-9. ')
rm -f "$REQ"
# shellcheck disable=SC2086
set -- $LINE
SENT=${1:-}
UP0=${2:-}
EPOCH=${SENT%%.*}

[ -n "$EPOCH" ] || { echo "settime: empty request" >&2; exit 1; }
if [ "$EPOCH" -lt "$MIN" ] || [ "$EPOCH" -gt "$MAX" ]; then
  echo "settime: $EPOCH is outside the accepted range" >&2
  exit 1
fi

# Time passed since the web app wrote the request (this helper takes a moment to start).
# The host's uptime is the same clock for both sides, so the difference is exact.
TARGET=$SENT
if [ -n "$UP0" ]; then
  UP1=$(cut -d' ' -f1 "${SHELLY_UPTIME_FILE:-/proc/uptime}")
  TARGET=$(awk -v t="$SENT" -v a="$UP0" -v b="$UP1" 'BEGIN { d = b - a; if (d < 0 || d > 60) d = 0; printf "%.3f", t + d }')
fi
case $TARGET in *[!0-9.]*|"") TARGET=$EPOCH ;; esac

date -u -s "@$TARGET" >/dev/null
echo "settime: system clock set to $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Keep the hardware clock (DS3231) in step, if there is one.
if [ -e /dev/rtc0 ]; then
  hwclock --systohc --utc && echo "settime: hardware clock updated"
fi
