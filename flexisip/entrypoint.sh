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
# cme-web (sharedline.py): prueft die Anmeldungen der Apps und bestaetigt Anfragen der CME an Flexisip
SHARED_LINE_PORT="${SHARED_LINE_PORT:-5074}"
# NICHT die LAN-IP: die ist ein Alias von Flexisip (aliases=...) - eine Route dorthin haelt Flexisip fuer
# "an mich", streicht sie und antwortet 482 Loop Detected bzw. sucht die Domain per DNS. cme-web ist ueber den
# veroeffentlichten Docker-Port auch auf 127.0.0.1 erreichbar.
SHARED_LINE_HOST="${SHARED_LINE_HOST:-127.0.0.1}"
LOG_LEVEL="${LOG_LEVEL:-message}"
src=/usr/local/share/flexisip/flexisip.conf.template
[ -f /etc/flexisip/flexisip.local.conf ] && src=/etc/flexisip/flexisip.local.conf
# Nur die eigenen Platzhalter ersetzen ($type, $token ... in external-push-uri gehören Flexisip)
sed -e "s|\${HOST_IP}|${HOST_IP}|g" -e "s|\${CME_IP}|${CME_IP}|g" -e "s|\${SIP_GW_NAME}|${SIP_GW_NAME}|g" \
    -e "s|\${SIP_PORT}|${SIP_PORT}|g" -e "s|\${RELAY_PORT}|${RELAY_PORT}|g" -e "s|\${LOG_LEVEL}|${LOG_LEVEL}|g" -e "s|\${SIP_DOMAIN}|${SIP_DOMAIN}|g" \
    "$src" > /run/flexisip.conf
# Routen (die erste passende gilt):
#  * REGISTER der Apps -> cme-web: prueft das Passwort des Mobilgeraete-Pools; Flexisip ist Registrar fuer
#    alle iPhones/iPads, CME sieht nur EINE Anmeldung je Nummer (die macht cme-web, Ziel = Flexisip)
#  * NOTIFY/REFER/OPTIONS der CME an Flexisip (service-control, Rufumleitung, Keepalive) -> cme-web bestaetigt
#  * Anrufe der Apps (SIP-Domain) und alles an die CME-IP -> CME (lr = Request-URI bleibt sip:...@<Domain>)
{
  echo "<sip:${SHARED_LINE_HOST}:${SHARED_LINE_PORT};transport=udp;lr>   request.method == 'REGISTER'"
  echo "<sip:${SHARED_LINE_HOST}:${SHARED_LINE_PORT};transport=udp;lr>   (request.method == 'NOTIFY' || request.method == 'REFER' || request.method == 'OPTIONS') && request.uri.domain == '${HOST_IP}'"
  echo "<sip:${CME_IP}:${CME_PORT};transport=udp;lr>   request.uri.domain == '${SIP_DOMAIN}'"
  echo "<sip:${CME_IP}:${CME_PORT};transport=udp;lr>   request.uri.domain == '${CME_IP}'"
} > /run/flexisip-routes.conf
echo "flexisip: $src -> /run/flexisip.conf (SIP ${HOST_IP}:${SIP_PORT}, Gateway '${SIP_GW_NAME}', Domain ${SIP_DOMAIN} -> CME ${CME_IP}:${CME_PORT}, Relay 127.0.0.1:${RELAY_PORT})"
# Passwoerter der Apps (module::Authentication): cme-web schreibt sie nach /data/push/flexisip-passwd (Volume
# softphone-data, hier nur lesbar). Flexisip liest eine Kopie in /run - fehlt die Datei oder ist sie kaputt,
# bricht Flexisip ab. Deshalb nur Dateien uebernehmen, die mit "version:1" beginnen; ohne Datei eine leere
# (dann klappen nur noch Anrufe von der CME zu den Apps, bis cme-web die Datei geschrieben hat).
PASSWD_SRC="${FLEXISIP_PASSWD:-/data/push/flexisip-passwd}"
[ -f /run/flexisip-passwd ] || printf 'version:1\n' > /run/flexisip-passwd
sync_passwd() {
  [ -f "$PASSWD_SRC" ] && [ "$(head -n 1 "$PASSWD_SRC")" = "version:1" ] || return 0
  # jede weitere Zeile: <user>@<domain> md5:<HA1> ; <anmeldename> (sonst bricht Flexisip ab)
  if tail -n +2 "$PASSWD_SRC" | grep -qvE '^[A-Za-z0-9._!%*+~-]+@[A-Za-z0-9.-]+ md5:[0-9a-f]{32} ; [A-Za-z0-9._!%*+~-]+$'; then
    echo "flexisip: $PASSWD_SRC hat ungueltige Zeilen - nicht uebernommen"
    return 0
  fi
  if ! cmp -s "$PASSWD_SRC" /run/flexisip-passwd; then
    cp "$PASSWD_SRC" /run/flexisip-passwd.tmp && chmod 600 /run/flexisip-passwd.tmp && mv /run/flexisip-passwd.tmp /run/flexisip-passwd
    echo "flexisip: Passwoerter aus $PASSWD_SRC uebernommen ($(grep -c '@' /run/flexisip-passwd) Eintraege)"
  fi
}
sync_passwd
( while sleep 20; do sync_passwd; done ) &
exec flexisip --server proxy -c /run/flexisip.conf "$@"
