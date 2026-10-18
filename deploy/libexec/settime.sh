#!/bin/sh
# Applies a "set the clock" request made from the web page.
#
# The web app runs in a container and cannot change the host's clock, so it
# drops a Unix timestamp into /var/lib/shelly-pi/bridge/settime.req.
# shelly-settime.path notices the file and starts this script as root.
set -eu

REQ=/var/lib/shelly-pi/bridge/settime.req
MIN=1767225600     # 2026-01-01, same lower bound as the app
MAX=4102444800     # 2100-01-01

[ -f "$REQ" ] || exit 0
# Digits only, at most 12 of them. Delete the request first so a failure can't loop.
EPOCH=$(head -c 12 "$REQ" | tr -cd '0-9')
rm -f "$REQ"

[ -n "$EPOCH" ] || { echo "settime: empty request" >&2; exit 1; }
if [ "$EPOCH" -lt "$MIN" ] || [ "$EPOCH" -gt "$MAX" ]; then
  echo "settime: $EPOCH is outside the accepted range" >&2
  exit 1
fi

date -u -s "@$EPOCH" >/dev/null
echo "settime: system clock set to $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Keep the hardware clock (DS3231) in step, if there is one.
if [ -e /dev/rtc0 ]; then
  hwclock --systohc --utc && echo "settime: hardware clock updated"
fi
