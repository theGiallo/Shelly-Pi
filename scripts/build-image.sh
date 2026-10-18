#!/usr/bin/env bash
# Build the arm64 container image on your PC and save it as shelly-monitor-image.tar,
# ready to copy to the Pi (setup-host.sh loads it automatically).
# Needs podman or docker (with buildx and arm64 emulation, e.g. Docker Desktop).
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=localhost/shelly-monitor:latest
OUT=shelly-monitor-image.tar

if command -v podman >/dev/null 2>&1; then
  podman build --platform linux/arm64 -t "$IMAGE" -f Containerfile .
  rm -f "$OUT"
  podman save -o "$OUT" "$IMAGE"
elif command -v docker >/dev/null 2>&1; then
  docker buildx build --platform linux/arm64 -t "$IMAGE" -f Containerfile \
    --output "type=docker,dest=$OUT" .
else
  echo "Install podman or docker first." >&2
  exit 1
fi
echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
