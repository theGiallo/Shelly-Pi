#!/bin/sh
# Installs a new app image that was uploaded from the Settings page.
#
# The web app runs in a container and can't replace itself. It saves the image to
# /var/lib/shelly-pi/bridge/update.tar and drops "<id> <sha256>" into update.req;
# shelly-update.path starts this script as root. Progress and the outcome go to
# update.res.json, which the page reads (also after the app has restarted).
#
# If the new version doesn't answer on /healthz within a minute and a half, the old image is put back.
set -u

BRIDGE=${SHELLY_BRIDGE:-/var/lib/shelly-pi/bridge}
ENVFILE=${SHELLY_ENVFILE:-/etc/shelly-pi/env}
IMAGE=localhost/shelly-pi
WAIT=${SHELLY_UPDATE_WAIT:-90}
REQ=$BRIDGE/update.req
RES=$BRIDGE/update.res.json
TAR=$BRIDGE/update.tar

[ -f "$REQ" ] || exit 0
LINE=$(head -c 200 "$REQ" | head -n 1)
rm -f "$REQ"
ID=$(printf '%s' "$LINE" | awk '{print $1}' | tr -cd 'A-Za-z0-9_-' | head -c 40)
WANT=$(printf '%s' "$LINE" | awk '{print $2}' | tr -cd '0-9a-f' | head -c 64)
[ -n "$ID" ] || { echo "update: request without an id" >&2; exit 1; }

json_str() {
  printf '"%s"' "$(printf '%s' "$1" | tr '\001-\037' ' ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}
state() { # state <name> <message>
  mkdir -p "$BRIDGE"
  printf '{"id":%s,"state":%s,"message":%s}\n' "$(json_str "$ID")" "$(json_str "$1")" "$(json_str "$2")" > "$RES.tmp" \
    && mv -f "$RES.tmp" "$RES"
  echo "update: $1: $2"
}
fail() { rm -f "$TAR"; state failed "$1"; exit 0; }

[ -f "$TAR" ] || fail "The uploaded image is missing."
[ "${#WANT}" -eq 64 ] || fail "The request had no checksum."
HAVE=$(sha256sum "$TAR" | awk '{print $1}')
[ "$HAVE" = "$WANT" ] || fail "The uploaded file doesn't match its checksum, so it was not installed."

state loading "Loading the new image."
HAD_OLD=0
if podman image exists "$IMAGE:latest" 2>/dev/null; then
  podman tag "$IMAGE:latest" "$IMAGE:previous" && HAD_OLD=1
fi
OUT=$(podman load -i "$TAR" 2>&1) || { rm -f "$TAR"; [ "$HAD_OLD" -eq 1 ] && podman tag "$IMAGE:previous" "$IMAGE:latest"; state failed "Podman couldn't load the image: $(printf '%s' "$OUT" | tail -n 1)"; exit 0; }
rm -f "$TAR"
case "$OUT" in
  *"$IMAGE:latest"*) ;;
  *) [ "$HAD_OLD" -eq 1 ] && podman tag "$IMAGE:previous" "$IMAGE:latest"
     state failed "That file isn't a Shelly-Pi image, so nothing was changed."; exit 0 ;;
esac

PORT=$([ -r "$ENVFILE" ] && sed -n 's/^SM_PORT=//p' "$ENVFILE" | tail -n 1 | tr -cd '0-9')
[ -n "$PORT" ] || PORT=80

restart_and_wait() {
  systemctl restart shelly-dhcp.service shelly-app.service
  n=0
  while [ "$n" -lt "$WAIT" ]; do
    if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then return 0; fi
    n=$((n + 1)); sleep 1
  done
  return 1
}

state restarting "Starting the new version."
if restart_and_wait; then
  state done "The new version is running."
  exit 0
fi

if [ "$HAD_OLD" -eq 1 ]; then
  podman tag "$IMAGE:previous" "$IMAGE:latest"
  if restart_and_wait; then
    state rolled_back "The new version didn't start, so the old one was put back."
  else
    state failed "The new version didn't start and the old one didn't either. Check: journalctl -u shelly-app -n 50"
  fi
else
  state failed "The new version didn't start. Check: journalctl -u shelly-app -n 50"
fi
exit 0
