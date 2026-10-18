# One image, two jobs: the web app (default command) and the DHCP server for
# the plugs' Wi-Fi (dnsmasq, started by the shelly-dhcp unit with run-dhcp).
#
# Build on the Pi (needs internet once):   podman build -t localhost/shelly-pi:latest .
# Or on a PC for the Pi:                   scripts/build-image.sh
FROM docker.io/library/python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SM_DATA_DIR=/data

# dnsmasq-base ships just the binary, with no service or config of its own.
RUN apt-get update \
 && apt-get install -y --no-install-recommends dnsmasq-base \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY deploy/run-dhcp.sh /usr/local/bin/run-dhcp
RUN chmod 0755 /usr/local/bin/run-dhcp

VOLUME /data
EXPOSE 80 443
CMD ["python", "-m", "app"]
