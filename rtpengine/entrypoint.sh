#!/bin/sh
# rtpengine für CME Phone: eine Schnittstelle (LAN-IP des Docker-Hosts), Steuerung nur lokal.
set -eu

: "${HOST_IP:?HOST_IP = LAN-IP des Docker-Hosts setzen}"
PORTS="${RTP_PORTS:-40000-40199}"
NG_PORT="${RTPENGINE_NG_PORT:-2223}"
LOG_LEVEL="${RTPENGINE_LOG_LEVEL:-6}"

echo "rtpengine: ${HOST_IP}, RTP ${PORTS}, Steuerung 127.0.0.1:${NG_PORT}"
exec rtpengine --foreground --log-stderr \
  --table=-1 \
  --interface="${HOST_IP}" \
  --listen-ng="127.0.0.1:${NG_PORT}" \
  --port-min="${PORTS%-*}" --port-max="${PORTS#*-}" \
  --log-level="${LOG_LEVEL}" \
  --timeout=60 --silent-timeout=21600 --final-timeout=43200 \
  --delete-delay=5
