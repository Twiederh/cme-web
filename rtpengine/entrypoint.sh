#!/bin/sh
# rtpengine für CME Phone: Schnittstelle "int" (LAN-IP des Docker-Hosts), Steuerung nur lokal.
# Mit SIP_PUBLIC_IP bzw. SIP_GW_NAME zusätzlich "ext": gleiche LAN-IP, im SDP aber die öffentliche Adresse
# (Anrufe aus dem Internet, Portweiterleitung UDP RTP_PORTS am Router). Kamailio wählt sie mit direction=ext.
set -eu

: "${HOST_IP:?HOST_IP = LAN-IP des Docker-Hosts setzen}"
PORTS="${RTP_PORTS:-40000-40199}"
NG_PORT="${RTPENGINE_NG_PORT:-2223}"
LOG_LEVEL="${RTPENGINE_LOG_LEVEL:-6}"

# Öffentliche Adresse: fest (SIP_PUBLIC_IP) oder SIP_GW_NAME über einen öffentlichen DNS-Server
# (intern zeigt der Name auf den Docker-Host)
public_ip() {
  if [ -n "${SIP_PUBLIC_IP:-}" ]; then echo "$SIP_PUBLIC_IP"; return; fi
  [ -n "${SIP_GW_NAME:-}" ] && [ -n "${SIP_PUBLIC_DOMAIN:-}" ] || return 0
  dig +short +time=3 +tries=2 @"${PUBLIC_DNS:-1.1.1.1}" A "$SIP_GW_NAME" 2>/dev/null \
    | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true
}
PUB=$(public_ip)
set -- --interface="int/${HOST_IP}"
if [ -n "$PUB" ]; then
  set -- "$@" --interface="ext/${HOST_IP}!${PUB}"
fi

echo "rtpengine: ${HOST_IP}${PUB:+, nach draußen als $PUB}, RTP ${PORTS}, Steuerung 127.0.0.1:${NG_PORT}"
rtpengine --foreground --log-stderr \
  --table=-1 \
  "$@" \
  --listen-ng="127.0.0.1:${NG_PORT}" \
  --port-min="${PORTS%-*}" --port-max="${PORTS#*-}" \
  --log-level="${LOG_LEVEL}" \
  --timeout=60 --silent-timeout=21600 --final-timeout=43200 \
  --delete-delay=5 &
RPID=$!
trap 'kill -TERM $RPID 2>/dev/null; wait $RPID; exit 0' TERM INT

# Öffentliche Adresse geändert (ohne SIP_PUBLIC_IP): neu starten, damit das SDP stimmt
while kill -0 $RPID 2>/dev/null; do
  sleep 300 &
  wait $! || true
  if [ -z "${SIP_PUBLIC_IP:-}" ] && [ -n "$PUB" ]; then
    NEW=$(public_ip)
    if [ -n "$NEW" ] && [ "$NEW" != "$PUB" ]; then
      echo "rtpengine: öffentliche Adresse jetzt $NEW (vorher $PUB) - Neustart"
      kill -TERM $RPID 2>/dev/null; wait $RPID || true
      exit 1
    fi
  fi
done
wait $RPID
