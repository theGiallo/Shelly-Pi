#!/usr/bin/env bash
# Runs once, on the Pi's first boot, started by shelly-firstboot.service. It was put on the SD card by
# scripts/prepare-sd.sh together with the project, the container image and firstboot.env (the answers).
# It runs the normal setup (deploy/setup-host.sh) without asking anything, then reboots once so the
# hardware clock overlay and the host name take effect.
#
# Log:  /var/lib/shelly-install/firstboot.log      Run again after a failure:  systemctl start shelly-firstboot
set -uo pipefail
DIR=/var/lib/shelly-install
exec > >(tee -a "$DIR/firstboot.log") 2>&1
echo "== Shelly-Pi first-boot setup, $(date -u '+%Y-%m-%d %H:%M:%S') UTC"

# shellcheck disable=SC1091
. "$DIR/firstboot.env"
rm -f "$DIR/failed"
cd "$DIR/project" || exit 1

# the Wi-Fi device can show up a little after NetworkManager starts
for _ in $(seq 1 60); do
  nmcli -t -f DEVICE,TYPE device 2>/dev/null | grep -q ':wifi$' && break
  sleep 2
done

export SHELLY_AP_PASSWORD="$AP_PASSWORD" SHELLY_ADMIN_PASSWORD="$ADMIN_PASSWORD"
args=(--yes --ssid "$SSID" --channel "$CHANNEL" --country "$COUNTRY" --hostname "$HOSTNAME_WANTED")
if [ "$RTC" = yes ]; then args+=(--rtc); else args+=(--no-rtc); fi

if bash deploy/setup-host.sh "${args[@]}"; then
  touch "$DIR/done"
  rm -f "$DIR/firstboot.env"            # the passwords stay only in /var/lib/shelly-pi/config.json
  ln -sfn "$DIR/project" /root/shelly-pi
  echo "== Setup finished. Rebooting in 10 seconds."
  sleep 10
  systemctl reboot
else
  touch "$DIR/failed"
  echo "== Setup failed. See above. After fixing the cause run: systemctl start shelly-firstboot" >&2
  exit 1
fi
