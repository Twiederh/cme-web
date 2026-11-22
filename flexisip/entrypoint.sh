#!/bin/sh
# Schreibt /etc/flexisip/flexisip.conf aus der Vorlage (Werte aus dem Stack) und startet Flexisip.
set -eu
: "${HOST_IP:?HOST_IP (VM_HOST_IP im Stack) = LAN-IP des Docker-Hosts setzen}"
SIP_GW_NAME="${SIP_GW_NAME:-}"
SIP_PORT="${SIP_PORT:-5062}"
RELAY_PORT="${RELAY_PORT:-8092}"
src=/usr/local/share/flexisip/flexisip.conf.template
[ -f /etc/flexisip/flexisip.local.conf ] && src=/etc/flexisip/flexisip.local.conf
# Nur die vier eigenen Platzhalter ersetzen ($type, $token ... in external-push-uri gehören Flexisip)
sed -e "s|\${HOST_IP}|${HOST_IP}|g" -e "s|\${SIP_GW_NAME}|${SIP_GW_NAME}|g" \
    -e "s|\${SIP_PORT}|${SIP_PORT}|g" -e "s|\${RELAY_PORT}|${RELAY_PORT}|g" \
    "$src" > /run/flexisip.conf
echo "flexisip: $src -> /run/flexisip.conf (SIP ${HOST_IP}:${SIP_PORT}, Gateway-Name '${SIP_GW_NAME}', Relay 127.0.0.1:${RELAY_PORT})"
exec flexisip --server proxy -c /run/flexisip.conf "$@"
