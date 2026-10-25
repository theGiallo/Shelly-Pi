#!/bin/sh
# Lists the USB drives plugged into the Pi and copies backups onto one of them.
#
# The web app runs in a container that cannot see the host's drives, so it drops
# a one-line request into /var/lib/shelly-pi/bridge/usb.req and
# shelly-usb.path starts this script as root. The answer goes to usb.res.json.
#
# Requests (the first word is an id the app uses to recognise its answer):
#   <id> list
#   <id> copy <filesystem-uuid> <file name> <how many backups to keep>
#
# Nothing is ever formatted or partitioned. A drive is mounted only while a backup
# is copied, then unmounted, so it can be unplugged at any other time. Backups go
# in the folder "shelly-pi-backups" and only files named
# shelly-pi_*.zip in that folder are ever deleted.
set -u
export LC_ALL=C   # awk handles bytes, not characters, so names pass through untouched

BRIDGE=${SHELLY_BRIDGE:-/var/lib/shelly-pi/bridge}
REQ=$BRIDGE/usb.req
RES=$BRIDGE/usb.res.json
OUT=$BRIDGE/outgoing
FOLDER=shelly-pi-backups
SUPPORTED="vfat exfat ntfs ext2 ext3 ext4 btrfs xfs"
MNT=${SHELLY_USB_MNT:-/run/shelly-usb}

[ -f "$REQ" ] || exit 0
LINE=$(head -c 400 "$REQ" | head -n 1)
rm -f "$REQ"

# ---- answers ---------------------------------------------------------------------

json_str() { # escape stdin-free argument as a JSON string
  # tr and sed behave the same everywhere; awk's gsub escaping differs between gawk (Fedora) and mawk (Debian)
  printf '"%s"' "$(printf '%s' "$1" | tr '\001-\037' ' ' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

ID=$(printf '%s' "$LINE" | awk '{print $1}' | tr -cd 'A-Za-z0-9_-' | head -c 40)
CMD=$(printf '%s' "$LINE" | awk '{print $2}')
[ -n "$ID" ] || { echo "usb: request without an id" >&2; exit 1; }

answer() { # answer <json body without braces>
  mkdir -p "$BRIDGE"
  printf '{"id":%s,%s}\n' "$(json_str "$ID")" "$1" > "$RES.tmp" && mv -f "$RES.tmp" "$RES"
}
fail() { answer "\"ok\":false,\"message\":$(json_str "$1")"; echo "usb: $1" >&2; exit 0; }

# ---- list ----------------------------------------------------------------------------

# One JSON object per partition (or bare disk) that is not part of the Pi's own storage.
list_json() {
  ${SHELLY_LSBLK:-lsblk} -P -b -o KNAME,TYPE,SIZE,FSTYPE,LABEL,UUID,MODEL,VENDOR,TRAN,HOTPLUG,PKNAME,MOUNTPOINT 2>/dev/null |
  awk -v supported="$SUPPORTED" '
    function hex(h,   d) {                         # ("0x" h) + 0 only works in mawk, not gawk (Fedora)
      d = "0123456789abcdef"
      return (index(d, tolower(substr(h, 1, 1))) - 1) * 16 + (index(d, tolower(substr(h, 2, 1))) - 1)
    }
    function unesc(s,   out, i, c, h) {            # lsblk writes odd bytes as \xNN
      out = ""
      while ((i = index(s, "\\x")) > 0) {
        h = substr(s, i + 2, 2)
        c = sprintf("%c", hex(h))
        out = out substr(s, 1, i - 1) c
        s = substr(s, i + 4)
      }
      return out s
    }
    function repl(s, from, to,   out, i) {           # plain text replace: gsub treats backslashes differently in gawk and mawk
      out = ""
      while ((i = index(s, from)) > 0) { out = out substr(s, 1, i - 1) to; s = substr(s, i + length(from)) }
      return out s
    }
    function js(s) { s = unesc(s); gsub(/[\001-\037]/, " ", s); s = repl(s, "\\", "\\\\"); s = repl(s, "\"", "\\\""); return "\"" s "\"" }
    function field(line, key,   re, v) {
      re = key "=\"[^\"]*\""
      if (match(line, re)) { v = substr(line, RSTART + length(key) + 2, RLENGTH - length(key) - 3); return v }
      return ""
    }
    BEGIN { split(supported, ok, " "); for (i in ok) good[ok[i]] = 1 }
    {
      rows[NR] = $0
    }
    END {
      for (n = 1; n <= NR; n++) {
        l = rows[n]
        kname = field(l, "KNAME"); type = field(l, "TYPE")
        parent[kname] = field(l, "PKNAME")
        hot[kname] = field(l, "HOTPLUG"); tran[kname] = field(l, "TRAN")
        model[kname] = field(l, "MODEL"); vendor[kname] = field(l, "VENDOR")
      }
      first = 1
      printf "["
      for (n = 1; n <= NR; n++) {
        l = rows[n]
        kname = field(l, "KNAME"); type = field(l, "TYPE"); fs = field(l, "FSTYPE"); uuid = field(l, "UUID")
        if ((type != "part" && type != "disk") || fs == "" || uuid == "") continue
        top = kname; while (parent[top] != "") top = parent[top]
        if (!(hot[top] == "1" || tran[top] == "usb")) continue       # SD card, internal disks: never offered
        mp = field(l, "MOUNTPOINT")
        if (mp == "/" || mp ~ /^\/(boot|var|usr|etc|sysroot)/) continue
        m = model[top]; v = vendor[top]; gsub(/^ +| +$/, "", m); gsub(/^ +| +$/, "", v)
        name = unesc(v " " m); gsub(/^ +| +$/, "", name)
        printf "%s{\"id\":%s,\"device\":%s,\"label\":%s,\"name\":%s,\"fstype\":%s,\"size\":%d,\"supported\":%s}",
          (first ? "" : ","), js(uuid), js("/dev/" kname), js(field(l, "LABEL")), js(name), js(fs),
          field(l, "SIZE") + 0, ((fs in good) ? "true" : "false")
        first = 0
      }
      printf "]"
    }'
}

if [ "$CMD" = list ]; then
  answer "\"ok\":true,\"drives\":$(list_json)"
  exit 0
fi

# ---- copy ----------------------------------------------------------------------------

[ "$CMD" = copy ] || fail "Unknown request."
UUID=$(printf '%s' "$LINE" | awk '{print $3}' | tr -cd 'A-Za-z0-9-' | head -c 64)
NAME=$(printf '%s' "$LINE" | awk '{print $4}')
KEEP=$(printf '%s' "$LINE" | awk '{print $5}' | tr -cd '0-9a-z' | head -c 4)
BACKUP_RE='^shelly-pi_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}(_v[0-9]+\.[0-9]+\.[0-9]+)?\.zip$'
printf '%s\n' "$NAME" | grep -Eq "$BACKUP_RE" || fail "That isn't a backup file name."
[ -n "$UUID" ] || fail "No drive was named."
if [ "$KEEP" != thin ]; then
  case $KEEP in ""|*[!0-9]*) fail "Keep between 1 and 3650 backups." ;; esac
  [ "$KEEP" -ge 1 ] && [ "$KEEP" -le 3650 ] || fail "Keep between 1 and 3650 backups."
fi
SRC=$OUT/$NAME
[ -f "$SRC" ] || fail "The backup file isn't there to copy."
SIZE=$(wc -c < "$SRC")

# Only a drive that the list above would offer can be written to.
DRIVE=$(list_json | tr '}' '\n' | grep -F "\"id\":\"$UUID\"" | head -n 1)
[ -n "$DRIVE" ] || fail "The drive isn't connected."
DEV=$(printf '%s' "$DRIVE" | sed -n 's/.*"device":"\([^"]*\)".*/\1/p')
FS=$(printf '%s' "$DRIVE" | sed -n 's/.*"fstype":"\([^"]*\)".*/\1/p')
printf '%s' "$DRIVE" | grep -q '"supported":true' || fail "This drive uses $FS, which the Pi can't write to safely. Use one formatted as exFAT, FAT32, NTFS or ext4."
[ -b "$DEV" ] || [ -n "${SHELLY_USB_FAKE:-}" ] || fail "The drive went away."

mkdir -p "$MNT"
OURS=0
if [ -n "${SHELLY_USB_FAKE:-}" ]; then
  MNT=$SHELLY_USB_FAKE                      # tests: a plain folder stands in for the drive
else
  EXISTING=$(findmnt -n -o TARGET --source "$DEV" 2>/dev/null | head -n 1)
  if [ -n "$EXISTING" ]; then
    MNT=$EXISTING                           # someone mounted it already: use it, leave it mounted
  else
    case "$FS" in
      ntfs) TYPE=ntfs3 ;;
      *) TYPE=$FS ;;
    esac
    # context= makes SELinux label every file; nodev,nosuid,noexec because only data goes here.
    ERR=$(mount -t "$TYPE" -o rw,nodev,nosuid,noexec,context=system_u:object_r:removable_t:s0 "$DEV" "$MNT" 2>&1) ||
      ERR=$(mount -t "$TYPE" -o rw,nodev,nosuid,noexec "$DEV" "$MNT" 2>&1) ||
      fail "The drive couldn't be opened: $(printf '%s' "$ERR" | tail -n 1). If it was unplugged without ejecting, plug it into a computer once and let it repair it."
    OURS=1
  fi
fi
cleanup() { [ "$OURS" = 1 ] && { sync; umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null; rmdir "$MNT" 2>/dev/null; }; return 0; }
trap cleanup EXIT

DIR=$MNT/$FOLDER
mkdir -p "$DIR" 2>/dev/null || fail "Couldn't make the folder $FOLDER on the drive. Is it write-protected?"
FREE=$(df -P -B1 "$DIR" | awk 'NR==2{print $4}')
[ -n "$FREE" ] && [ "$FREE" -gt $((SIZE + 1048576)) ] || fail "The drive is full: a backup needs $((SIZE / 1048576 + 1)) MB."

rm -f "$DIR/.$NAME.part"
if ! ERR=$(cp "$SRC" "$DIR/.$NAME.part" 2>&1); then
  rm -f "$DIR/.$NAME.part"
  fail "Copying failed: $(printf '%s' "$ERR" | tail -n 1)"
fi
sync
mv -f "$DIR/.$NAME.part" "$DIR/$NAME" || fail "Couldn't finish the copy."
[ "$(wc -c < "$DIR/$NAME")" -eq "$SIZE" ] || { rm -f "$DIR/$NAME"; fail "The copy on the drive is the wrong size."; }

# Keep the newest $KEEP, by name (names sort by date). Nothing else in the folder is touched.
is_backup() { printf '%s\n' "$1" | grep -Eq "$BACKUP_RE"; }
COUNT=0
for f in "$DIR"/shelly-pi_*.zip; do
  if [ -f "$f" ] && is_backup "${f##*/}"; then COUNT=$((COUNT + 1)); fi
done
REMOVED=0
if [ "$KEEP" = thin ]; then
  # Spread over time: a backup is kept when it is the first one in a time window whose length grows with its age
  # (1 h, 2 h, 4 h, 8 h, then 1 d, 2 d, 4 d, 8 d, 16 d, 32 d ... doubling), and nothing older than two years stays.
  # The windows divide each other, so a backup kept at one age is still the right one to keep at the next.
  # Ages are counted from the newest backup, and the times come from the file names.
  LIST=""
  for f in "$DIR"/shelly-pi_*.zip; do
    if [ -f "$f" ] && is_backup "${f##*/}"; then
      b=${f##*/}; d=${b#shelly-pi_}
      t=$(printf '%s' "$d" | cut -c1-10)" "$(printf '%s' "$d" | cut -c12-19 | tr '-' ':')
      e=$(date -u -d "$t" +%s 2>/dev/null) || continue
      LIST="$LIST$e $b
"
    fi
  done
  DROP=$(printf '%s' "$LIST" | awk '
    NF == 2 { e[NR] = $1; n[NR] = $2; if ($1 > newest) newest = $1; total = NR }
    END {
      split("1 2 4 8 24 48 96 192 384 768 1536 3072 6144 12288", w, " ")
      for (i = 1; i <= total; i++) {
        age = (newest - e[i]) / 3600
        win = 1
        for (k = 1; k in w; k++) if (w[k] <= age) win = w[k]
        key[i] = win ":" int(e[i] / (win * 3600))
        if (!(key[i] in first) || e[i] < first[key[i]]) first[key[i]] = e[i]
      }
      for (i = 1; i <= total; i++) {
        age = (newest - e[i]) / 3600
        if (e[i] == newest) continue
        if (age > 17520 || e[i] != first[key[i]]) print n[i]
      }
    }')
  for b in $DROP; do
    if is_backup "$b" && rm -f "$DIR/$b"; then REMOVED=$((REMOVED + 1)); fi
  done
elif [ "$COUNT" -gt "$KEEP" ]; then
  EXCESS=$((COUNT - KEEP))
  for f in "$DIR"/shelly-pi_*.zip; do        # the shell lists them in name order
    [ "$EXCESS" -gt 0 ] || break
    if [ -f "$f" ] && is_backup "${f##*/}"; then rm -f "$f"; EXCESS=$((EXCESS - 1)); REMOVED=$((REMOVED + 1)); fi
  done
fi
sync
LEFT=$(df -P -B1 "$DIR" | awk 'NR==2{print $4}')
answer "\"ok\":true,\"file\":$(json_str "$NAME"),\"bytes\":$SIZE,\"free\":${LEFT:-0},\"removed\":$REMOVED,\"message\":\"Copied.\""
echo "usb: copied $NAME ($SIZE bytes) to $DEV"
