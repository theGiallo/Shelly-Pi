#!/bin/sh
# DHCP-only dnsmasq for the Wi-Fi network the Pi hosts. Runs inside the container
# (unit: shelly-dhcp). DNS is switched off (--port=0): the plugs don't need it.
#
#  * Leases are written to /data/dnsmasq/dnsmasq.leases, which the web app reads
#    to spot new plugs.
#  * Address reservations are files in /data/dhcp-hosts.d/, one per plug, written
#    by the web app. dnsmasq watches that folder, so no restart is needed.
set -eu

IFACE="${SM_AP_IFACE:-wlan0}"
AP_IP="${SM_AP_IP:-192.168.50.1}"
START="${SM_DHCP_START:-192.168.50.100}"
END="${SM_DHCP_END:-192.168.50.199}"
DATA="${SM_DATA_DIR:-/data}"

mkdir -p "$DATA/dnsmasq" "$DATA/dhcp-hosts.d"

exec dnsmasq \
  --keep-in-foreground \
  --conf-file=/dev/null \
  --user=root --group=root \
  --port=0 \
  --interface="$IFACE" --bind-interfaces \
  --dhcp-range="$START,$END,255.255.255.0,12h" \
  --dhcp-authoritative \
  --dhcp-option="option:router,$AP_IP" \
  --dhcp-leasefile="$DATA/dnsmasq/dnsmasq.leases" \
  --dhcp-hostsdir="$DATA/dhcp-hosts.d" \
  --log-facility=-
