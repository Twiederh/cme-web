#!/bin/sh
# Kamailio für CME Phone: Konfiguration aus der Vorlage, Passwörter der Apps aus cme-web, dann starten.
set -eu

: "${HOST_IP:?HOST_IP = LAN-IP des Docker-Hosts setzen}"
: "${CME_IP:?CME_IP (CME_HOST im Stack) = IP der Anlage setzen}"
CME_PORT="${CME_PORT:-5060}"
SIP_DOMAIN="${SIP_DOMAIN:-cme.local}"
# Port der Apps (wie bisher Flexisip) und Port zur CME (Contact des Pools MOB<Nummer>, cme-web: B2BUA_PORT)
APP_PORT="${SIP_PORT:-5062}"
CME_SIDE_PORT="${CME_SIDE_PORT:-${B2BUA_PORT:-5064}}"
RPC_PORT="${RPC_PORT:-5069}"
SHARED_LINE_PORT="${SHARED_LINE_PORT:-5074}"
WAIT_PORT="${WAIT_PORT:-5099}"
RELAY_PORT="${RELAY_PORT:-8092}"
RTPENGINE_NG_PORT="${RTPENGINE_NG_PORT:-2223}"
# Medien zu den Apps: SRTP (SDES) – APP_SRTP=0 schaltet auf unverschlüsseltes RTP
if [ "${APP_SRTP:-1}" = "0" ]; then APP_MEDIA="RTP/AVP"; else APP_MEDIA="RTP/SAVP"; fi
# Log: 1 = Fehler/Warnungen, 2 = Info (Standard), 3 = Debug
LOG_LEVEL="${KAMAILIO_LOG_LEVEL:-2}"
DATA="${KAMAILIO_DATA:-/data/kamailio}"
DBDIR="$DATA/db"
PASSWD="${PASSWD_FILE:-/data/push/flexisip-passwd}"

mkdir -p /run/kamailio "$DBDIR"
# db_text-Tabellen (usrloc, topos) beim ersten Start aus dem Paket
SCHEMA=/usr/share/kamailio/dbtext/kamailio
if [ -d "$SCHEMA" ]; then
  for t in version location location_attrs topos_d topos_t; do
    [ -f "$DBDIR/$t" ] || { [ -f "$SCHEMA/$t" ] && cp "$SCHEMA/$t" "$DBDIR/$t"; } || true
  done
fi
USRLOC_DB_MODE=0
[ -f "$DBDIR/location" ] && [ -f "$DBDIR/version" ] && USRLOC_DB_MODE=1

# Passwörter: ohne gültige Datei leere Tabellen (keine App kommt durch, Kamailio läuft trotzdem)
if ! python3 /usr/local/bin/passwd2htable.py "$PASSWD" "$DBDIR" >/dev/null; then
  echo "kamailio: $PASSWD fehlt oder ist ungültig - noch keine App-Passwörter"
  for t in subs_ha1 subs_num; do
    [ -f "$DBDIR/$t" ] || printf 'key_name(str) key_type(int) value_type(int) key_value(str) expires(int)\n' > "$DBDIR/$t"
  done
fi

HOST_IP_RE=$(printf '%s' "$HOST_IP" | sed 's/\./\\\\./g')
sed -e "s|\${HOST_IP}|${HOST_IP}|g" -e "s|\${HOST_IP_RE}|${HOST_IP_RE}|g" -e "s|\${CME_IP}|${CME_IP}|g" \
    -e "s|\${CME_PORT}|${CME_PORT}|g" -e "s|\${SIP_DOMAIN}|${SIP_DOMAIN}|g" -e "s|\${APP_PORT}|${APP_PORT}|g" \
    -e "s|\${CME_SIDE_PORT}|${CME_SIDE_PORT}|g" -e "s|\${RPC_PORT}|${RPC_PORT}|g" -e "s|\${WAIT_PORT}|${WAIT_PORT}|g" \
    -e "s|\${RELAY_PORT}|${RELAY_PORT}|g" -e "s|\${SHARED_LINE_PORT}|${SHARED_LINE_PORT}|g" -e "s|\${RTPENGINE_NG_PORT}|${RTPENGINE_NG_PORT}|g" \
    -e "s|\${APP_MEDIA}|${APP_MEDIA}|g" -e "s|\${LOG_LEVEL}|${LOG_LEVEL}|g" -e "s|\${DBDIR}|${DBDIR}|g" \
    -e "s|\${USRLOC_DB_MODE}|${USRLOC_DB_MODE}|g" \
    /usr/local/share/kamailio/kamailio.cfg.template > /run/kamailio/kamailio.cfg
if grep -n '\${' /run/kamailio/kamailio.cfg; then
  echo "kamailio: Platzhalter nicht ersetzt (siehe oben)" >&2
  exit 1
fi
echo "kamailio: Apps ${HOST_IP}:${APP_PORT}, CME ${CME_IP}:${CME_PORT} über ${HOST_IP}:${CME_SIDE_PORT}, Medien zu den Apps: ${APP_MEDIA%% *}, Anmeldungen ${USRLOC_DB_MODE:+gespeichert (db_mode $USRLOC_DB_MODE)}"

[ "${1:-}" = "check" ] && exec kamailio -c -f /run/kamailio/kamailio.cfg

kamailio -DD -E -f /run/kamailio/kamailio.cfg &
KPID=$!
trap 'kill -TERM $KPID 2>/dev/null; wait $KPID; exit 0' TERM INT

# Neue Passwörter von cme-web übernehmen (alle 20 s), dann die htables neu laden
while kill -0 $KPID 2>/dev/null; do
  sleep 20 &
  wait $! || true
  if out=$(python3 /usr/local/bin/passwd2htable.py "$PASSWD" "$DBDIR" 2>/dev/null) && [ "$out" = "changed" ]; then
    kamcmd -s unix:/run/kamailio/kamailio_ctl htable.reload ha1 >/dev/null 2>&1 || true
    kamcmd -s unix:/run/kamailio/kamailio_ctl htable.reload num >/dev/null 2>&1 || true
    echo "kamailio: App-Passwörter aktualisiert"
  fi
done
wait $KPID
