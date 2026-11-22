#!/bin/sh
# Schreibt /etc/flexisip/flexisip.conf aus der Vorlage (Werte aus dem Stack) und startet Flexisip.
set -eu
: "${HOST_IP:?HOST_IP (VM_HOST_IP im Stack) = LAN-IP des Docker-Hosts setzen}"
: "${CME_IP:?CME_IP (CME_HOST im Stack) = IP der Anlage setzen}"
CME_PORT="${CME_PORT:-5060}"
SIP_DOMAIN="${SIP_DOMAIN:-cme.local}"
SIP_GW_NAME="${SIP_GW_NAME:-}"
SIP_PORT="${SIP_PORT:-5062}"
RELAY_PORT="${RELAY_PORT:-8092}"
LOG_LEVEL="${LOG_LEVEL:-message}"
src=/usr/local/share/flexisip/flexisip.conf.template
[ -f /etc/flexisip/flexisip.local.conf ] && src=/etc/flexisip/flexisip.local.conf
# Nur die eigenen Platzhalter ersetzen ($type, $token ... in external-push-uri gehören Flexisip)
sed -e "s|\${HOST_IP}|${HOST_IP}|g" -e "s|\${SIP_GW_NAME}|${SIP_GW_NAME}|g" \
    -e "s|\${SIP_PORT}|${SIP_PORT}|g" -e "s|\${RELAY_PORT}|${RELAY_PORT}|g" -e "s|\${LOG_LEVEL}|${LOG_LEVEL}|g" -e "s|\${SIP_DOMAIN}|${SIP_DOMAIN}|g" \
    "$src" > /run/flexisip.conf
# Route zu CME für die SIP-Domain der Apps (lr = Request-URI bleibt sip:...@<Domain>) und für die CME-IP
{
  echo "<sip:${CME_IP}:${CME_PORT};transport=udp;lr>   request.uri.domain == '${SIP_DOMAIN}'"
  echo "<sip:${CME_IP}:${CME_PORT};transport=udp;lr>   request.uri.domain == '${CME_IP}'"
} > /run/flexisip-routes.conf
echo "flexisip: $src -> /run/flexisip.conf (SIP ${HOST_IP}:${SIP_PORT}, Gateway '${SIP_GW_NAME}', Domain ${SIP_DOMAIN} -> CME ${CME_IP}:${CME_PORT}, Relay 127.0.0.1:${RELAY_PORT})"
exec flexisip --server proxy -c /run/flexisip.conf "$@"
