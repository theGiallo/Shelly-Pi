#!/usr/bin/env bash
# Prepare a ready-to-boot SD card for the Raspberry Pi 4: Fedora IoT, with Shelly-Pi already
# installed and its Wi-Fi access point already configured. Run it on a Fedora PC, as root:
#
#     sudo bash scripts/prepare-sd.sh --media /dev/sdX
#
# What it does
#   1. downloads the newest Fedora IoT aarch64 image for the release (or uses --image FILE) and checks it
#   2. builds the app's container image on this PC (or uses --app-image FILE)
#   3. writes the SD card with arm-image-installer (EVERYTHING ON THE CARD IS ERASED)
#   4. copies the project, the container image and your answers onto the card, with a one-time service
#      that sets everything up when the Pi first boots (no internet needed on the Pi)
#   5. saves the passwords it generated next to you in sd-credentials_<date>.txt
#
# Options
#   --media DEV            the SD card, e.g. /dev/sdb (required unless --inject-only)
#   --image FILE           a Fedora-IoT ...aarch64.raw.xz you already have
#   --release N            Fedora release to download (default 44)
#   --ssh-key FILE         public key allowed to log in as root (default: your id_ed25519/ecdsa/rsa .pub)
#   --ssid NAME            Wi-Fi name for the plugs (default ShellyPi)
#   --ap-password PW       Wi-Fi password, 8-63 characters (default: generated)
#   --admin-password PW    dashboard password for user admin (default: generated)
#   --hostname NAME        the Pi's host name (default shellypi)
#   --channel N            Wi-Fi channel 1-11 (default 6)      --country CC   (default IT)
#   --no-rtc               no DS3231 clock module
#   --app-image FILE       shelly-pi-image.tar built earlier (default: build it here)
#   --cache DIR            where downloads are kept (default ~/.cache/shelly-pi)
#   --yes                  don't ask for confirmation before erasing the card
#   --inject-only DIR      skip download and writing: only copy the setup files into an already mounted
#                          Fedora IoT root file system at DIR (for testing, or if you flash it yourself)
set -euo pipefail
export LC_ALL=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE_URL="https://dl.fedoraproject.org/pub/alt/iot"          # directory listings and checksums
MIRROR_URL="https://download.fedoraproject.org/pub/alt/iot"   # the big download, redirected to a nearby mirror

MEDIA="" IMAGE="" RELEASE=44 SSH_KEY="" SSID="ShellyPi" AP_PASSWORD="" ADMIN_PASSWORD=""
HOSTNAME_WANTED="shellypi" CHANNEL=6 COUNTRY=IT RTC=yes APP_IMAGE="" CACHE="" ASSUME_YES=0 INJECT_ONLY=""

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
die()  { printf '\nError: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

gen_secret() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# ---- pieces that can be tested without a card ------------------------------------------------------

# latest_from_listing FILE RELEASE: newest image name in an HTML directory listing. Fedora names them
# Fedora-IoT-raw-44-20260427.0.aarch64.raw.xz (older releases: Fedora-IoT-44-...), so both are accepted.
latest_from_listing() {
  grep -o "Fedora-IoT-\(raw-\)\{0,1\}$2-[0-9.]*\\.aarch64\\.raw\\.xz" "$1" | sort -u |
    sed -E 's/^(.*-)([0-9]{8}\.[0-9]+)\.aarch64\.raw\.xz$/\2 &/' | sort -V | tail -n 1 | cut -d' ' -f2
}

# checksum_file_from_listing FILE RELEASE IMAGE: the CHECKSUM file that goes with that image's build
# (Fedora-IoT-44-aarch64-20260427.0-CHECKSUM; also the older spellings)
checksum_file_from_listing() {
  local build; build="$(printf '%s' "$3" | sed -E 's/.*-([0-9]{8}\.[0-9]+)\.aarch64\.raw\.xz$/\1/')"
  grep -o "Fedora-IoT-$2-aarch64-$build-CHECKSUM\\|Fedora-IoT-$2-$build-aarch64-CHECKSUM\\|Fedora-IoT-$2-aarch64-CHECKSUM" "$1" | sort -u | head -n 1
}

# expected_sha256 CHECKSUMFILE IMAGE: the hash for IMAGE, from lines like "SHA256 (name) = hash"
expected_sha256() {
  awk -v f="$2" '$1=="SHA256" && $2=="("f")" {print $4}' "$1" | head -n 1
}

# write_env FILE: the answers the Pi reads on first boot, shell-quoted
write_env() {
  : > "$1"; chmod 600 "$1"
  {
    printf 'SSID=%q\n' "$SSID"
    printf 'AP_PASSWORD=%q\n' "$AP_PASSWORD"
    printf 'ADMIN_PASSWORD=%q\n' "$ADMIN_PASSWORD"
    printf 'HOSTNAME_WANTED=%q\n' "$HOSTNAME_WANTED"
    printf 'CHANNEL=%q\n' "$CHANNEL"
    printf 'COUNTRY=%q\n' "$COUNTRY"
    printf 'RTC=%q\n' "$RTC"
  } >> "$1"
}

relabel() { # relabel TYPE PATH...: give files the SELinux type the Pi expects (best effort)
  local type="$1"; shift
  if command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled && command -v chcon >/dev/null 2>&1; then
    chcon -R -t "$type" "$@" 2>/dev/null || warn "Couldn't set the SELinux label $type on $*. If the first-boot setup doesn't start, see the README."
  fi
}

# inject ROOT: copy the project, the app image, the answers and the first-boot service into an
# ostree root file system mounted at ROOT
inject() {
  local root="$1" stateroot dep var
  [ -d "$root/ostree/deploy" ] || die "$root doesn't look like a Fedora IoT root (no ostree/deploy)."
  local roots=("$root"/ostree/deploy/*/)
  [ "${#roots[@]}" -eq 1 ] || die "Expected one OS state under ostree/deploy, found ${#roots[@]}."
  stateroot="${roots[0]%/}"
  local deps=("$stateroot"/deploy/*.[0-9])
  [ -d "${deps[0]}" ] || die "No deployment found under $stateroot/deploy."
  dep="$(printf '%s\n' "${deps[@]}" | sort -V | tail -n 1)"
  var="$stateroot/var"
  [ -d "$var" ] || die "No var directory under $stateroot."

  local target="$var/lib/shelly-install"
  rm -rf "$target"
  install -d -m 0700 "$target" "$target/project"
  tar -C "$PROJECT" \
      --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=demo-data \
      --exclude='*.tar.gz' --exclude='sd-credentials_*' --exclude='shelly-pi-image.tar' -cf - . \
    | tar -C "$target/project" -xf -
  if [ -n "$APP_IMAGE" ]; then
    cp "$APP_IMAGE" "$target/project/shelly-pi-image.tar"
  else
    warn "No container image was copied: the Pi will have to build it, which needs internet on the first boot."
  fi
  write_env "$target/firstboot.env"
  relabel var_lib_t "$target"

  local unit_dir="$dep/etc/systemd/system"
  install -d "$unit_dir/multi-user.target.wants"
  install -m 0644 "$PROJECT/deploy/firstboot/shelly-firstboot.service" "$unit_dir/shelly-firstboot.service"
  ln -sfn /etc/systemd/system/shelly-firstboot.service "$unit_dir/multi-user.target.wants/shelly-firstboot.service"
  relabel systemd_unit_file_t "$unit_dir/shelly-firstboot.service"
  note "Setup files are on the card ($(du -sh "$target" | cut -f1))."
}

# ---- main ------------------------------------------------------------------------------------------

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --media) MEDIA="${2:?--media needs a device}"; shift 2 ;;
      --image) IMAGE="${2:?}"; shift 2 ;;
      --release) RELEASE="${2:?}"; shift 2 ;;
      --ssh-key) SSH_KEY="${2:?}"; shift 2 ;;
      --ssid) SSID="${2-}"; shift 2 ;;
      --ap-password) AP_PASSWORD="${2:?}"; shift 2 ;;
      --admin-password) ADMIN_PASSWORD="${2:?}"; shift 2 ;;
      --hostname) HOSTNAME_WANTED="${2:?}"; shift 2 ;;
      --channel) CHANNEL="${2:?}"; shift 2 ;;
      --country) COUNTRY="${2:?}"; shift 2 ;;
      --no-rtc) RTC=no; shift ;;
      --app-image) APP_IMAGE="${2:?}"; shift 2 ;;
      --cache) CACHE="${2:?}"; shift 2 ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      --inject-only) INJECT_ONLY="${2:?}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (try --help)" ;;
    esac
  done

  # settings every mode needs
  [ "${#SSID}" -ge 1 ] && [ "${#SSID}" -le 32 ] || die "The Wi-Fi name must be 1 to 32 characters."
  case "$CHANNEL" in 1|2|3|4|5|6|7|8|9|10|11) ;; *) die "--channel must be between 1 and 11" ;; esac
  [[ "$HOSTNAME_WANTED" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || die "The host name may use a-z, 0-9 and '-'."
  [[ "$COUNTRY" =~ ^[A-Z]{2}$ ]] || die "--country is a two-letter code such as IT."
  local generated=()
  if [ -z "$AP_PASSWORD" ]; then AP_PASSWORD="$(gen_secret 10)"; generated+=("Plugs' Wi-Fi password: $AP_PASSWORD"); fi
  if [ "${#AP_PASSWORD}" -lt 8 ] || [ "${#AP_PASSWORD}" -gt 63 ]; then die "The Wi-Fi password must be 8 to 63 characters."; fi
  if [ -z "$ADMIN_PASSWORD" ]; then ADMIN_PASSWORD="$(gen_secret 8)"; generated+=("Dashboard password (user admin): $ADMIN_PASSWORD"); fi
  if [ -n "$APP_IMAGE" ] && [ ! -f "$APP_IMAGE" ]; then die "--app-image: $APP_IMAGE doesn't exist."; fi

  if [ -n "$INJECT_ONLY" ]; then
    inject "$INJECT_ONLY"
    save_credentials "${generated[@]+"${generated[@]}"}"
    return 0
  fi

  [ "$(id -u)" -eq 0 ] || die "Run this as root: sudo bash scripts/prepare-sd.sh --media /dev/sdX"
  [ -n "$MEDIA" ] || die "Say which device is the SD card with --media /dev/sdX (see 'lsblk')."
  for c in arm-image-installer xz lsblk curl sha256sum mount tar; do
    command -v "$c" >/dev/null 2>&1 || die "'$c' is missing. On Fedora: sudo dnf install arm-image-installer xz util-linux curl coreutils"
  done

  check_media
  find_ssh_key
  fetch_image
  build_app_image

  say "Writing Fedora IoT to $MEDIA"
  arm-image-installer --image="$IMAGE" --media="$MEDIA" --target=rpi4 \
    --addkey="$SSH_KEY" --norootpass --resizefs -y

  say "Adding Shelly-Pi"
  udevadm settle 2>/dev/null || true
  partprobe "$MEDIA" 2>/dev/null || true
  sleep 2
  local mnt; mnt="$(mktemp -d)"
  local part
  part="$(lsblk -lnpb -o NAME,TYPE,FSTYPE,SIZE "$MEDIA" | awk '$2=="part" && ($3=="xfs"||$3=="btrfs"||$3=="ext4") {print $4, $1}' | sort -n | tail -n 1 | cut -d' ' -f2)"
  [ -n "$part" ] || die "Couldn't find the root partition on $MEDIA after writing. Nothing was added; the card still has plain Fedora IoT."
  mount "$part" "$mnt"
  if [ ! -d "$mnt/ostree" ] && [ -d "$mnt/root/ostree" ]; then umount "$mnt"; mount -o subvol=root "$part" "$mnt"; fi
  trap 'umount "$mnt" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true' EXIT
  inject "$mnt"
  sync
  umount "$mnt"; rmdir "$mnt"; trap - EXIT
  sync
  save_credentials "${generated[@]+"${generated[@]}"}"

  say "The SD card is ready"
  note "1. Put it in the Pi, connect Ethernet to your router, power on."
  note "2. First boot sets everything up and reboots once: allow about 10 minutes."
  note "3. Find the Pi's address in your router (or try http://$HOSTNAME_WANTED.local/), open it, user admin."
  note "   Progress:  ssh root@$HOSTNAME_WANTED.local journalctl -fu shelly-firstboot"
  [ "$RTC" = yes ] && note "4. With the Pi powered off, fit the DS3231 module before the first boot (see the README)."
}

check_media() {
  [ -b "$MEDIA" ] || die "$MEDIA isn't a block device."
  [ "$(lsblk -dno TYPE "$MEDIA")" = "disk" ] || die "$MEDIA is a partition. Give the whole disk, for example /dev/sdb."
  if lsblk -lno MOUNTPOINTS "$MEDIA" | grep -q '[^[:space:]]'; then
    die "$MEDIA has mounted partitions. Unmount them first (or you picked the wrong device)."
  fi
  local rootsrc
  rootsrc="$(findmnt -no SOURCE / 2>/dev/null || true)"
  if [ -n "$rootsrc" ] && lsblk -lnpo NAME "$MEDIA" | grep -qx "$rootsrc"; then
    die "$MEDIA holds this computer's own system. Refusing."
  fi
  say "The card"
  lsblk -o NAME,SIZE,MODEL,TRAN,RM "$MEDIA"
  if [ "$(lsblk -dno RM "$MEDIA")" != "1" ]; then
    warn "$MEDIA isn't marked removable. Be very sure it is the SD card, and not a disk with your data."
  fi
  if [ "$ASSUME_YES" -ne 1 ]; then
    printf '\nEverything on %s will be erased. Type the device name to continue: ' "$MEDIA"
    local reply; read -r reply
    [ "$reply" = "$MEDIA" ] || die "Not confirmed. Nothing was changed."
  fi
}

find_ssh_key() {
  if [ -z "$SSH_KEY" ]; then
    local home="${SUDO_USER:+$(getent passwd "$SUDO_USER" | cut -d: -f6)}"; home="${home:-$HOME}"
    for k in id_ed25519.pub id_ecdsa.pub id_rsa.pub; do
      if [ -f "$home/.ssh/$k" ]; then SSH_KEY="$home/.ssh/$k"; break; fi
    done
  fi
  [ -n "$SSH_KEY" ] && [ -f "$SSH_KEY" ] || die "No SSH public key found. Make one with: ssh-keygen -t ed25519   (or pass --ssh-key FILE)"
  note "SSH key for root: $SSH_KEY"
}

fetch_image() {
  if [ -n "$IMAGE" ]; then
    [ -f "$IMAGE" ] || die "--image: $IMAGE doesn't exist."
    note "Using $IMAGE"
    return 0
  fi
  local cache="${CACHE:-${SUDO_USER:+$(getent passwd "$SUDO_USER" | cut -d: -f6)}}"
  cache="${cache:-$HOME}"; [ -n "$CACHE" ] || cache="$cache/.cache/shelly-pi"
  mkdir -p "$cache"
  local url="$BASE_URL/$RELEASE/IoT/aarch64/images/"
  say "Looking for the newest Fedora IoT $RELEASE image"
  local listing="$cache/listing-$RELEASE.html"
  curl -fsSL "$url" -o "$listing" || die "Couldn't read $url. Download the image yourself from https://fedoraproject.org/iot/ and pass --image FILE."
  local name; name="$(latest_from_listing "$listing" "$RELEASE")"
  [ -n "$name" ] || die "No Fedora-IoT-$RELEASE aarch64 .raw.xz image in that listing. Pass --image FILE."
  note "Found $name"
  IMAGE="$cache/$name"
  if [ ! -f "$IMAGE" ] || [ -f "$IMAGE.part" ]; then
    if curl -fL -C - --progress-bar "$MIRROR_URL/$RELEASE/IoT/aarch64/images/$name" -o "$IMAGE.part" ||
       curl -fL -C - --progress-bar "$url$name" -o "$IMAGE.part"; then
      mv "$IMAGE.part" "$IMAGE"
    else
      die "The download failed. Run the command again to resume it."
    fi
  else
    note "Already downloaded."
  fi
  local sumfile; sumfile="$(checksum_file_from_listing "$listing" "$RELEASE" "$name")"
  if [ -z "$sumfile" ]; then
    warn "No checksum file found for this image, so it can't be verified."
    [ "$ASSUME_YES" -eq 1 ] || { printf 'Continue anyway? [y/N] '; local r; read -r r; [ "$r" = y ] || die "Stopped."; }
    return 0
  fi
  curl -fsSL "$url$sumfile" -o "$cache/$sumfile" || die "Couldn't download $sumfile."
  local want have
  want="$(expected_sha256 "$cache/$sumfile" "$name")"
  [ -n "$want" ] || die "$sumfile has no SHA256 for $name."
  have="$(sha256sum "$IMAGE" | cut -d' ' -f1)"
  if [ "$want" != "$have" ]; then
    rm -f "$IMAGE"
    die "The downloaded image doesn't match its checksum, so it was deleted. Run the command again."
  fi
  note "Checksum OK."
  local key="/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-$RELEASE-primary"
  if command -v gpg >/dev/null 2>&1 && [ -f "$key" ]; then
    local gh; gh="$(mktemp -d)"
    gpg --homedir "$gh" --batch --quiet --import "$key" 2>/dev/null
    if gpg --homedir "$gh" --batch --quiet --verify "$cache/$sumfile" 2>/dev/null; then
      note "The checksum file's signature is valid (Fedora release key)."
    else
      rm -rf "$gh"; die "The checksum file's signature is NOT valid. Not continuing."
    fi
    rm -rf "$gh"
  else
    warn "The checksum file's signature wasn't checked (no gpg or no Fedora key for release $RELEASE here). The image matches its checksum, which only proves the download wasn't damaged."
  fi
}

build_app_image() {
  if [ -n "$APP_IMAGE" ]; then note "Using the app image $APP_IMAGE"; return 0; fi
  if [ -f "$PROJECT/shelly-pi-image.tar" ]; then
    APP_IMAGE="$PROJECT/shelly-pi-image.tar"; note "Using $APP_IMAGE"; return 0
  fi
  say "Building the app's container image for the Pi (arm64)"
  note "This needs podman or docker, arm64 emulation (Fedora: sudo dnf install podman qemu-user-static) and internet."
  bash "$SCRIPT_DIR/build-image.sh" || die "The image couldn't be built. Build it elsewhere and pass --app-image FILE."
  APP_IMAGE="$PROJECT/shelly-pi-image.tar"
}

save_credentials() {
  [ "$#" -gt 0 ] || return 0
  local f; f="sd-credentials_$(date '+%Y-%m-%d_%H-%M-%S').txt"
  {
    echo "Shelly-Pi, set up $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Plugs' Wi-Fi name: $SSID"
    echo "Host name: $HOSTNAME_WANTED   (http://$HOSTNAME_WANTED.local/, user admin)"
    for g in "$@"; do echo "$g"; done
  } > "$f"
  chmod 600 "$f"
  [ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$f" 2>/dev/null || true
  say "Passwords were generated"
  for g in "$@"; do note "$g"; done
  note "Also saved in ./$f. Keep it somewhere safe."
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
