#!/bin/sh
# Lists the Wi-Fi networks the Pi can see, for the "Add plug" page.
#
# The web app runs in a container and can't scan, so it drops "<id> scan" into
# /var/lib/shelly-monitor/bridge/wifiscan.req; shelly-wifiscan.path starts this script as
# root; the answer goes to wifiscan.res.json. Nothing is changed on the Pi.
#
# Scanning uses the interface in SM_SCAN_IFACE, or else SM_AP_IFACE, from
# /etc/shelly-monitor/env (the Wi-Fi the Pi also hosts its own network on). Scanning while
# hosting works on most Pi Wi-Fi chips but can fail on some; a second USB Wi-Fi adapter
# named in SM_SCAN_IFACE avoids the question.
set -u

BRIDGE=${SHELLY_BRIDGE:-/var/lib/shelly-monitor/bridge}
ENVFILE=${SHELLY_ENVFILE:-/etc/shelly-monitor/env}
REQ=$BRIDGE/wifiscan.req
RES=$BRIDGE/wifiscan.res.json

[ -f "$REQ" ] || exit 0
LINE=$(head -c 200 "$REQ" | head -n 1)
rm -f "$REQ"
ID=$(printf '%s' "$LINE" | awk '{print $1}' | tr -cd 'A-Za-z0-9_-' | head -c 40)
[ -n "$ID" ] || { echo "wifiscan: request without an id" >&2; exit 1; }

json_str() {
  printf '%s' "$1" | awk 'BEGIN{ORS=""; printf "\""} {gsub(/\\/,"\\\\"); gsub(/"/,"\\\""); gsub(/[\001-\037]/," "); if (NR>1) printf " "; printf "%s",$0} END{printf "\""}'
}
answer() {
  mkdir -p "$BRIDGE"
  printf '{"id":%s,%s}\n' "$(json_str "$ID")" "$1" > "$RES.tmp" && mv -f "$RES.tmp" "$RES"
}
fail() { answer "\"ok\":false,\"message\":$(json_str "$1")"; echo "wifiscan: $1" >&2; exit 0; }

envval() { [ -r "$ENVFILE" ] && sed -n "s/^$1=//p" "$ENVFILE" | tail -n 1 | tr -cd 'A-Za-z0-9_.:-'; }
IFACE=${SHELLY_SCAN_IFACE:-$(envval SM_SCAN_IFACE)}
[ -n "$IFACE" ] || IFACE=$(envval SM_AP_IFACE)
[ -n "$IFACE" ] || IFACE=wlan0

# ---- get a scan, in either of two formats --------------------------------------------------

FORMAT=iw
if [ -n "${SHELLY_WIFISCAN_CMD:-}" ]; then            # tests and demos: a command that prints a scan
  FORMAT=${SHELLY_WIFISCAN_FORMAT:-iw}
  RAW=$($SHELLY_WIFISCAN_CMD 2>&1) || fail "The scan command failed."
elif command -v iw >/dev/null 2>&1; then
  # ap-force lets the scan run while this interface is hosting a network
  RAW=$(timeout 25 iw dev "$IFACE" scan ap-force 2>&1) ||
    RAW=$(timeout 25 iw dev "$IFACE" scan 2>&1) ||
    fail "The Pi couldn't scan for Wi-Fi networks on $IFACE while it hosts its own network: $(printf '%s' "$RAW" | tail -n 1)"
elif command -v nmcli >/dev/null 2>&1; then
  FORMAT=nmcli
  RAW=$(timeout 25 nmcli --escape no -t -f BSSID,CHAN,SIGNAL,SECURITY,SSID device wifi list ifname "$IFACE" --rescan yes 2>&1) ||
    fail "The Pi couldn't scan for Wi-Fi networks on $IFACE: $(printf '%s' "$RAW" | tail -n 1)"
else
  fail "Neither iw nor nmcli is installed on the Pi, so it can't scan."
fi

# ---- turn it into one tab-separated line per network: ssid bssid dBm channel secured --------

parse_iw() {
  awk '
    function flush() { if (bssid != "" && ssid != "") printf "%s\t%s\t%s\t%s\t%s\n", ssid, bssid, sig, chan, sec }
    /^BSS / { flush(); bssid = substr($2, 1, 17); ssid = ""; sig = -100; chan = 0; sec = 0; next }
    /^[ \t]+freq:/ { f = $2 + 0; chan = (f < 3000) ? int((f - 2407) / 5) : int((f - 5000) / 5); next }
    /^[ \t]+signal:/ { sig = $2 + 0; next }
    /^[ \t]+capability:/ { if ($0 ~ /Privacy/) sec = 1; next }
    /^[ \t]+(RSN|WPA):/ { sec = 1; next }
    /^[ \t]+SSID:/ { s = $0; sub(/^[ \t]+SSID: ?/, "", s); ssid = s; next }
    END { flush() }'
}
parse_nmcli() {
  awk '
    {
      bssid = substr($0, 1, 17); rest = substr($0, 19)
      n = split(rest, a, ":")
      chan = a[1] + 0; pct = a[2] + 0; sec = (a[3] == "" || a[3] == "--") ? 0 : 1
      ssid = a[4]; for (i = 5; i <= n; i++) ssid = ssid ":" a[i]
      if (ssid != "") printf "%s\t%s\t%d\t%d\t%d\n", ssid, bssid, pct / 2 - 100, chan, sec
    }'
}

if [ "$FORMAT" = nmcli ]; then TSV=$(printf '%s\n' "$RAW" | parse_nmcli); else TSV=$(printf '%s\n' "$RAW" | parse_iw); fi

NETS=$(printf '%s\n' "$TSV" | awk -F'\t' '
  function unesc(s,   out, i, h) {                 # iw writes odd bytes as \xNN
    out = ""
    while ((i = index(s, "\\x")) > 0) { h = substr(s, i + 2, 2); out = out substr(s, 1, i - 1) sprintf("%c", ("0x" h) + 0); s = substr(s, i + 4) }
    return out s
  }
  function js(s) { s = unesc(s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); gsub(/[\001-\037]/, " ", s); return "\"" s "\"" }
  NF >= 5 && $1 != "" && count < 200 {
    printf "%s{\"ssid\":%s,\"bssid\":%s,\"signal\":%d,\"channel\":%d,\"secured\":%s}",
      (count ? "," : ""), js($1), js($2), $3, $4, ($5 == 1 ? "true" : "false")
    count++
  }')

answer "\"ok\":true,\"iface\":$(json_str "$IFACE"),\"networks\":[$NETS]"
