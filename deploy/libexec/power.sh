#!/bin/sh
# Shuts the Pi down or restarts it, on request from the Settings page.
#
# The web app runs in a container and can't switch the Pi off. It drops "<id> poweroff" or
# "<id> reboot" into /var/lib/shelly-pi/bridge/power.req; shelly-power.path starts this script
# as root. It waits a few seconds first, so the page has time to show its message.
set -u

BRIDGE=${SHELLY_BRIDGE:-/var/lib/shelly-pi/bridge}
DELAY=${SHELLY_POWER_DELAY:-3}
REQ=$BRIDGE/power.req

[ -f "$REQ" ] || exit 0
LINE=$(head -c 100 "$REQ" | head -n 1)
# A request that has been lying around (the helper was not installed then) must not fire now.
STALE=$(find "$REQ" -mmin +1 2>/dev/null)
rm -f "$REQ"
if [ -n "$STALE" ]; then echo "power: ignoring an old request" >&2; exit 0; fi
ACTION=$(printf '%s' "$LINE" | awk '{print $2}')

case "$ACTION" in
  poweroff|reboot) ;;
  *) echo "power: refusing '$ACTION'" >&2; exit 1 ;;
esac

echo "power: $ACTION in $DELAY seconds"
sleep "$DELAY"
systemctl "$ACTION"
