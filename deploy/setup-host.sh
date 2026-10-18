#!/usr/bin/env bash
# First-time setup of the Raspberry Pi 4 (Fedora IoT). Run as root, from the project folder:
#
#     sudo bash deploy/setup-host.sh
#
# Safe to run again: it reuses the saved passwords and just reapplies everything.
#
# What it does
#   1. loads (or builds) the container image
#   2. saves your passwords and settings
#   3. creates the Wi-Fi access point for the plugs (NetworkManager)
#   4. opens the firewall for the dashboard
#   5. installs the clock helpers (and, optionally, the DS3231 hardware clock)
#   6. installs and starts the two containers: web app and DHCP server
#
# Options:  --ssid NAME  --channel N  --country CC  --iface wlan0  --ap-ip 192.168.50.1
#           --hostname NAME  --no-rtc  --rtc  --rebuild  --new-passwords  --yes
#           --uninstall [--purge]
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
IMAGE="localhost/shelly-pi:latest"
STATE=/var/lib/shelly-pi
CONF=/etc/shelly-pi
LIBEXEC=/usr/local/libexec/shelly-pi
QUADLETS=/etc/containers/systemd
AP_CON=shelly-ap

SSID=""
CHANNEL=6
COUNTRY=IT
IFACE=wlan0
AP_IP=192.168.50.1
NEW_HOSTNAME=shellypi
USE_RTC=ask
REBUILD=0
NEW_PASSWORDS=0
ASSUME_YES=0
UNINSTALL=0
PURGE=0

WARNINGS=()
NEED_REBOOT=0
GENERATED=()

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; WARNINGS+=("$*"); }
die()  { printf '\nError: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ssid) SSID="${2:?--ssid needs a value}"; shift 2 ;;
    --channel) CHANNEL="${2:?}"; shift 2 ;;
    --country) COUNTRY="${2:?}"; shift 2 ;;
    --iface) IFACE="${2:?}"; shift 2 ;;
    --ap-ip) AP_IP="${2:?}"; shift 2 ;;
    --hostname) NEW_HOSTNAME="${2:?}"; shift 2 ;;
    --no-rtc) USE_RTC=no; shift ;;
    --rtc) USE_RTC=yes; shift ;;
    --rebuild) REBUILD=1; shift ;;
    --new-passwords) NEW_PASSWORDS=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --purge) PURGE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1 (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run this as root: sudo bash deploy/setup-host.sh"

# ---- helpers ------------------------------------------------------------------

interactive() { [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; }

ask() { # ask "Question" "default"
  local reply
  if interactive; then
    read -r -p "$1${2:+ [$2]}: " reply
    echo "${reply:-${2:-}}"
  else
    echo "${2:-}"
  fi
}

ask_secret() { # ask_secret "Question"  (empty answer = empty string)
  local reply
  if interactive; then
    read -r -s -p "$1: " reply
    echo >&2
    echo "$reply"
  fi
}

gen_secret() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

cfg_get() { # cfg_get key  -> value from the saved config.json (read inside the image)
  podman run --rm -v "$STATE:/data:z" "$IMAGE" \
    python -c 'import json,sys; print(json.load(open("/data/config.json")).get(sys.argv[1], ""))' "$1"
}

# ---- uninstall ---------------------------------------------------------------------

if [ "$UNINSTALL" -eq 1 ]; then
  say "Removing Shelly-Pi"
  systemctl disable --now shelly-app.service shelly-dhcp.service shelly-settime.path shelly-usb.path shelly-wifiscan.path 2>/dev/null || true
  rm -f "$QUADLETS/shelly-app.container" "$QUADLETS/shelly-dhcp.container"
  rm -f /etc/systemd/system/shelly-settime.path /etc/systemd/system/shelly-settime.service \
        /etc/systemd/system/shelly-usb.path /etc/systemd/system/shelly-usb.service \
        /etc/systemd/system/shelly-wifiscan.path /etc/systemd/system/shelly-wifiscan.service \
        /etc/systemd/system/rtc-load.service /etc/udev/rules.d/90-shelly-rtc.rules
  rm -rf "$LIBEXEC" "$CONF"
  nmcli connection delete "$AP_CON" >/dev/null 2>&1 || true
  systemctl daemon-reload
  if [ "$PURGE" -eq 1 ]; then
    rm -rf "$STATE"
    note "Deleted all data in $STATE."
  else
    note "Kept your data in $STATE (add --purge to delete it)."
  fi
  note "Edits to the boot config (config.txt) for the clock module were left alone."
  exit 0
fi

# ---- 0. checks -------------------------------------------------------------------------

say "Checking this system"
for c in podman nmcli systemctl curl; do
  command -v "$c" >/dev/null 2>&1 || die "'$c' is missing. This script is written for Fedora IoT."
done
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  note "OS: ${PRETTY_NAME:-unknown}"
  case "${VARIANT_ID:-}${ID:-}" in
    *iot*|fedora) ;;
    *) warn "This isn't Fedora; the script may need changes." ;;
  esac
fi
[ "$(uname -m)" = "aarch64" ] || warn "This machine is $(uname -m), not aarch64 (a 64-bit Pi OS)."
nmcli -t -f DEVICE,TYPE device | grep -q "^${IFACE}:wifi$" \
  || die "No Wi-Fi interface called '$IFACE'. Run 'nmcli device' to see yours, then use --iface."
case "$CHANNEL" in
  1|2|3|4|5|6|7|8|9|10|11) ;;
  *) die "--channel must be between 1 and 11" ;;
esac

# ---- 1. image ------------------------------------------------------------------------------

say "Container image"
if [ "$REBUILD" -eq 1 ] || ! podman image exists "$IMAGE"; then
  if [ "$REBUILD" -eq 0 ] && [ -f "$ROOT/shelly-pi-image.tar" ]; then
    note "Loading $ROOT/shelly-pi-image.tar"
    podman load -i "$ROOT/shelly-pi-image.tar"
  else
    note "Building the image here. This needs internet access (about 60 MB)."
    podman build -t "$IMAGE" -f "$ROOT/Containerfile" "$ROOT" \
      || die "Build failed. No internet? Build on your PC with scripts/build-image.sh and copy shelly-pi-image.tar into $ROOT."
  fi
else
  note "$IMAGE is already here (use --rebuild to rebuild it)."
fi

# ---- 2. settings and passwords ----------------------------------------------------------------

say "Settings"
install -d -m 0755 "$STATE" "$STATE/dhcp-hosts.d" "$STATE/dnsmasq" "$STATE/bridge" "$CONF"
AP_PASS="" ADMIN_PASS=""
if [ -f "$STATE/config.json" ] && [ "$NEW_PASSWORDS" -eq 0 ]; then
  note "Reusing the saved settings in $STATE/config.json (--new-passwords to change them)."
  [ -n "$SSID" ] || SSID="$(cfg_get ap_ssid)"
  AP_PASS="$(cfg_get ap_password)"
  ADMIN_PASS="$(cfg_get password)"
fi
# Preset by scripts/prepare-sd.sh for the unattended first boot.
[ -n "$AP_PASS" ] || AP_PASS="${SHELLY_AP_PASSWORD:-}"
[ -n "$ADMIN_PASS" ] || ADMIN_PASS="${SHELLY_ADMIN_PASSWORD:-}"
if [ -z "$SSID" ]; then SSID="$(ask "Name for the plugs' Wi-Fi network" "ShellyPi")"; fi
[ -n "$SSID" ] || SSID="ShellyPi"
[ "${#SSID}" -le 32 ] || die "The Wi-Fi name can be at most 32 characters."
if [ -z "$AP_PASS" ]; then
  AP_PASS="$(ask_secret "Password for the plugs' Wi-Fi, 8 to 63 characters (Enter to generate one)")"
  if [ -z "$AP_PASS" ]; then AP_PASS="$(gen_secret 10)"; GENERATED+=("Plugs' Wi-Fi password: $AP_PASS"); fi
fi
if [ "${#AP_PASS}" -lt 8 ] || [ "${#AP_PASS}" -gt 63 ]; then die "The Wi-Fi password must be 8 to 63 characters."; fi
if [ -z "$ADMIN_PASS" ]; then
  ADMIN_PASS="$(ask_secret "Password for the dashboard, user 'admin' (Enter to generate one)")"
  if [ -z "$ADMIN_PASS" ]; then ADMIN_PASS="$(gen_secret 8)"; GENERATED+=("Dashboard password (user admin): $ADMIN_PASS"); fi
fi

NET="${AP_IP%.*}"
SM_PASSWORD="$ADMIN_PASS" SM_AP_PASSWORD="$AP_PASS" \
  podman run --rm -e SM_PASSWORD -e SM_AP_PASSWORD -v "$STATE:/data:z" "$IMAGE" \
  python -m app.cli init-config --data-dir /data --ap-ssid "$SSID" --ap-ip "$AP_IP"
chmod 600 "$STATE/config.json"

cat > "$CONF/env" <<EOF
# Written by setup-host.sh. Secrets live in $STATE/config.json instead.
SM_AP_IFACE=$IFACE
SM_AP_IP=$AP_IP
SM_DHCP_START=$NET.100
SM_DHCP_END=$NET.199
SM_PORT=80
EOF
chmod 644 "$CONF/env"

# ---- 3. access point ------------------------------------------------------------------------------

say "Wi-Fi access point '$SSID' on $IFACE (channel $CHANNEL, 2.4 GHz)"
nmcli radio wifi on || true
command -v rfkill >/dev/null 2>&1 && rfkill unblock wifi || true
nmcli connection delete "$AP_CON" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$IFACE" con-name "$AP_CON" autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel "$CHANNEL" \
  ipv4.method manual ipv4.addresses "$AP_IP/24" ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PASS" \
  wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
  connection.zone trusted connection.autoconnect-priority 100 >/dev/null
if command -v iw >/dev/null 2>&1; then iw reg set "$COUNTRY" 2>/dev/null || true; fi
if [ ! -e /etc/modprobe.d/cfg80211.conf ]; then
  echo "options cfg80211 ieee80211_regdom=$COUNTRY" > /etc/modprobe.d/cfg80211.conf
fi
if nmcli connection up "$AP_CON" >/dev/null 2>&1; then
  note "Access point is up at $AP_IP."
else
  warn "The access point didn't start. Check: journalctl -u NetworkManager -n 50 (the Pi 4's Wi-Fi needs its firmware, which Fedora ships)."
fi

# ---- 4. firewall -------------------------------------------------------------------------------------

if systemctl is-active --quiet firewalld; then
  say "Firewall"
  firewall-cmd --permanent --add-service=http >/dev/null
  firewall-cmd --permanent --add-service=https >/dev/null
  firewall-cmd --permanent --add-service=mdns >/dev/null 2>&1 || warn "Couldn't open mDNS, so shellypi.local may not resolve"
  firewall-cmd --reload >/dev/null
  note "Ports 80 and 443 are open for the dashboard, and mDNS (for the shellypi.local name). The Wi-Fi for the plugs is in the 'trusted' zone."
fi

# ---- 5. clock helpers -------------------------------------------------------------------------------------

say "Clock"
install -d -m 0755 "$LIBEXEC"
install -m 0755 "$HERE/libexec/settime.sh" "$LIBEXEC/settime.sh"
install -m 0644 "$HERE/systemd/shelly-settime.path" "$HERE/systemd/shelly-settime.service" /etc/systemd/system/
install -m 0755 "$HERE/libexec/usb.sh" "$LIBEXEC/usb.sh"
install -m 0644 "$HERE/systemd/shelly-usb.path" "$HERE/systemd/shelly-usb.service" /etc/systemd/system/
install -m 0755 "$HERE/libexec/wifiscan.sh" "$LIBEXEC/wifiscan.sh"
install -m 0644 "$HERE/systemd/shelly-wifiscan.path" "$HERE/systemd/shelly-wifiscan.service" /etc/systemd/system/
command -v iw >/dev/null 2>&1 || warn "The 'iw' tool isn't installed, so the Add plug page will scan with nmcli, which may not work while the Pi hosts its network."
command -v restorecon >/dev/null 2>&1 && restorecon -R "$LIBEXEC" || true
note "The 'Set the Pi's clock' button and the USB backup drive picker on the Settings page are ready."

if [ "$USE_RTC" = ask ]; then
  case "$(ask "Is the DS3231 clock module connected to the Pi? (y/n)" "y")" in
    [Yy]*) USE_RTC=yes ;;
    *) USE_RTC=no ;;
  esac
fi
if [ "$USE_RTC" = yes ]; then
  BOOTCFG=""
  for f in /boot/efi/config.txt /boot/firmware/config.txt /boot/config.txt; do
    if [ -f "$f" ]; then BOOTCFG="$f"; break; fi
  done
  if [ -z "$BOOTCFG" ]; then
    warn "Couldn't find the Pi's config.txt. Add 'dtparam=i2c_arm=on' and 'dtoverlay=i2c-rtc,ds3231' to it by hand."
  else
    if ! grep -q '^dtoverlay=i2c-rtc,ds3231' "$BOOTCFG"; then
      [ -f "$BOOTCFG.shelly-bak" ] || cp "$BOOTCFG" "$BOOTCFG.shelly-bak"
      printf '\n# Shelly-Pi: DS3231 hardware clock\ndtparam=i2c_arm=on\ndtoverlay=i2c-rtc,ds3231\n' >> "$BOOTCFG"
      note "Added the DS3231 lines to $BOOTCFG."
      NEED_REBOOT=1
    else
      note "$BOOTCFG already has the DS3231 lines."
    fi
    ls "$(dirname "$BOOTCFG")"/overlays/i2c-rtc* >/dev/null 2>&1 \
      || warn "No i2c-rtc overlay found next to $BOOTCFG; the clock module may not be recognised."
  fi
  install -m 0644 "$HERE/systemd/rtc-load.service" /etc/systemd/system/
  install -m 0644 "$HERE/udev/90-shelly-rtc.rules" /etc/udev/rules.d/
  udevadm control --reload || true
  if [ -e /dev/rtc0 ]; then
    note "Hardware clock found: $(cat /sys/class/rtc/rtc0/name 2>/dev/null || echo rtc0)"
  else
    note "No hardware clock visible yet. That's expected until after the reboot."
    NEED_REBOOT=1
  fi
fi
systemctl daemon-reload
systemctl enable --now shelly-settime.path >/dev/null 2>&1 || warn "Couldn't enable shelly-settime.path"
systemctl enable --now shelly-usb.path >/dev/null 2>&1 || warn "Couldn't enable shelly-usb.path"
systemctl enable --now shelly-wifiscan.path >/dev/null 2>&1 || warn "Couldn't enable shelly-wifiscan.path"

# ---- 6. containers --------------------------------------------------------------------------------------------

say "Starting the services"
install -d -m 0755 "$QUADLETS"
install -m 0644 "$HERE/quadlet/shelly-app.container" "$HERE/quadlet/shelly-dhcp.container" "$QUADLETS/"
systemctl daemon-reload
systemctl restart shelly-dhcp.service shelly-app.service

CURRENT_HOST="$(hostnamectl --static 2>/dev/null || hostname)"
if [ "$CURRENT_HOST" != "$NEW_HOSTNAME" ]; then
  if [ "$(ask "Change this Pi's hostname from '$CURRENT_HOST' to '$NEW_HOSTNAME'? (y/n)" "y")" = y ]; then
    hostnamectl set-hostname "$NEW_HOSTNAME" && note "Hostname is now $NEW_HOSTNAME."
  else
    NEW_HOSTNAME="$CURRENT_HOST"
  fi
fi
# The app runs in a container, so it can't see the Pi's host name; tell it, so the HTTPS certificate is made for
# the real name (and not for "localhost").
if grep -q '^SM_HOSTNAME=' "$CONF/env"; then
  sed -i "s/^SM_HOSTNAME=.*/SM_HOSTNAME=$NEW_HOSTNAME/" "$CONF/env"
else
  echo "SM_HOSTNAME=$NEW_HOSTNAME" >> "$CONF/env"
fi
systemctl restart shelly-app.service

# Answer "<hostname>.local" lookups from the home network.
if systemctl list-unit-files systemd-resolved.service >/dev/null 2>&1; then
  install -d -m 0755 /etc/systemd/resolved.conf.d
  printf '[Resolve]\nMulticastDNS=yes\n' > /etc/systemd/resolved.conf.d/shelly-mdns.conf
  systemctl enable --now systemd-resolved >/dev/null 2>&1 || true
  systemctl restart systemd-resolved >/dev/null 2>&1 || true
fi
ETH_CON="$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="802-3-ethernet"{print $1; exit}')"
if [ -n "$ETH_CON" ]; then
  nmcli connection modify "$ETH_CON" connection.mdns yes >/dev/null 2>&1 \
    && nmcli connection up "$ETH_CON" >/dev/null 2>&1 || true
fi

printf '    Waiting for the dashboard'
UP=0
for _ in $(seq 1 40); do
  if curl -fsS -o /dev/null http://127.0.0.1/healthz 2>/dev/null; then UP=1; break; fi
  printf '.'; sleep 1
done
echo
if [ "$UP" -eq 1 ]; then note "The dashboard is running."; else warn "The dashboard didn't answer. Check: journalctl -u shelly-app -n 50"; fi
sleep 2
if ! systemctl is-active --quiet shelly-dhcp.service; then
  warn "The DHCP server isn't running. Check: journalctl -u shelly-dhcp -n 50"
fi

# ---- summary ---------------------------------------------------------------------------------------------------

ETH_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk -v i="$IFACE" '$2!=i {split($4,a,"/"); print a[1]; exit}')"
ETH_SHOW="${ETH_IP:-ETHERNET-ADDRESS-OF-THE-PI}"
say "Done"
note "Dashboard from your PC:   http://$ETH_SHOW/   (or http://$NEW_HOSTNAME.local/)"
note "From the plugs' Wi-Fi:    http://$AP_IP/"
note "HTTPS (optional):         https://$NEW_HOSTNAME.local/  after installing the Pi's certificate, see Settings"
note "User name: admin"
if [ "${#GENERATED[@]}" -gt 0 ]; then
  echo
  echo "    Write these down (they are also stored in $STATE/config.json):"
  for g in "${GENERATED[@]}"; do note "$g"; done
fi
if [ "$NEED_REBOOT" -eq 1 ]; then
  echo
  note "Reboot now to switch on the hardware clock:  sudo systemctl reboot"
  note "Afterwards open Settings in the dashboard and press 'Set the Pi's clock'."
fi
if [ "${#WARNINGS[@]}" -gt 0 ]; then
  echo
  echo "  Things to look at:"
  for w in "${WARNINGS[@]}"; do note "- $w"; done
fi
