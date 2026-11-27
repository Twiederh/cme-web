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
# Zugang von außen (TCP/TLS unter SIP_GW_NAME, Adressen <name>@SIP_PUBLIC_DOMAIN): nur mit Domain und Zertifikat
PUBLIC_DOMAIN="${SIP_PUBLIC_DOMAIN:-}"
EXT_NAME="${SIP_GW_NAME:-}"
EXT_TCP_PORT="${EXT_SIP_PORT:-5060}"
EXT_TLS_PORT="${EXT_TLS_PORT:-5061}"
EXT_UDP_PORT="${EXT_UDP_PORT:-5067}"
EXT_ADV_UDP="${EXT_UDP_PUBLIC_PORT:-5060}"
# Ports, die draußen ankommen (Portweiterleitung am Router), stehen in Via/Record-Route
EXT_ADV_TCP="${EXT_SIP_PUBLIC_PORT:-5060}"
EXT_ADV_TLS="${EXT_TLS_PUBLIC_PORT:-5061}"
CME_EXT_PORT="${CME_EXT_PORT:-5066}"
ALIASES="${ALIASES_FILE:-/data/push/sip-aliases}"
ACME_JSON="${ACME_JSON:-/data/acme/acme.json}"
TLSDIR=/run/kamailio/tls

mkdir -p /run/kamailio "$DBDIR"
# db_text-Tabellen (usrloc, topos) beim ersten Start aus dem Paket
SCHEMA=/usr/share/kamailio/dbtext/kamailio
if [ -d "$SCHEMA" ]; then
  for t in version location location_attrs topos_d topos_t; do
    [ -f "$DBDIR/$t" ] || { [ -f "$SCHEMA/$t" ] && cp "$SCHEMA/$t" "$DBDIR/$t"; } || true
  done
fi
# topos lässt Spalten leer (z.B. x_context) – db_text lehnt die Zeile sonst ab ("null value not allowed"),
# der Anruf kommt dann nicht zur App. Alle Spalten außer id dürfen leer sein (auch für vorhandene Tabellen).
for t in topos_d topos_t; do
  [ -f "$DBDIR/$t" ] && sed -i '1s/(\(string\|int\))/(\1,null)/g' "$DBDIR/$t"
done
USRLOC_DB_MODE=0
[ -f "$DBDIR/location" ] && [ -f "$DBDIR/version" ] && USRLOC_DB_MODE=1

# Passwörter: ohne gültige Datei leere Tabellen (keine App kommt durch, Kamailio läuft trotzdem)
if ! python3 /usr/local/bin/passwd2htable.py "$PASSWD" "$DBDIR" >/dev/null; then
  echo "kamailio: $PASSWD fehlt oder ist ungültig - noch keine App-Passwörter"
  for t in subs_ha1 subs_num; do
    [ -f "$DBDIR/$t" ] || printf 'key_name(str) key_type(int) value_type(int) key_value(str) expires(int)\n' > "$DBDIR/$t"
  done
fi

python3 /usr/local/bin/passwd2htable.py --aliases "$ALIASES" "$DBDIR" >/dev/null || true

# Zertifikat: eigene Dateien in TLS_CERT_DIR (cert.pem, key.pem) oder aus der acme.json von Traefik
cert_update() {
  if [ -n "${TLS_CERT_DIR:-}" ] && [ -f "$TLS_CERT_DIR/cert.pem" ] && [ -f "$TLS_CERT_DIR/key.pem" ]; then
    mkdir -p "$TLSDIR"
    if ! cmp -s "$TLS_CERT_DIR/cert.pem" "$TLSDIR/cert.pem" || ! cmp -s "$TLS_CERT_DIR/key.pem" "$TLSDIR/key.pem"; then
      cp "$TLS_CERT_DIR/cert.pem" "$TLSDIR/cert.pem"; cp "$TLS_CERT_DIR/key.pem" "$TLSDIR/key.pem"
      chmod 600 "$TLSDIR/key.pem"; echo changed
    else
      echo unchanged
    fi
  else
    python3 /usr/local/bin/acme2pem.py "$ACME_JSON" "$EXT_NAME" "$TLSDIR"
  fi
}
EXT=0
if [ -n "$PUBLIC_DOMAIN" ] && [ -n "$EXT_NAME" ]; then
  if cert_update >/dev/null; then
    EXT=1
  else
    echo "kamailio: kein Zertifikat für $EXT_NAME (ACME_JSON $ACME_JSON bzw. TLS_CERT_DIR) - kein Zugang von außen"
  fi
fi
cat > /run/kamailio/tls.cfg <<TLS
[server:default]
method = TLSv1.2+
verify_certificate = no
require_certificate = no
private_key = $TLSDIR/key.pem
certificate = $TLSDIR/cert.pem

[client:default]
method = TLSv1.2+
verify_certificate = no
require_certificate = no
TLS

HOST_IP_RE=$(printf '%s' "$HOST_IP" | sed 's/\./\\\\./g')
sed -e "s|\${HOST_IP}|${HOST_IP}|g" -e "s|\${HOST_IP_RE}|${HOST_IP_RE}|g" -e "s|\${CME_IP}|${CME_IP}|g" \
    -e "s|\${CME_PORT}|${CME_PORT}|g" -e "s|\${SIP_DOMAIN}|${SIP_DOMAIN}|g" -e "s|\${APP_PORT}|${APP_PORT}|g" \
    -e "s|\${CME_SIDE_PORT}|${CME_SIDE_PORT}|g" -e "s|\${RPC_PORT}|${RPC_PORT}|g" -e "s|\${WAIT_PORT}|${WAIT_PORT}|g" \
    -e "s|\${RELAY_PORT}|${RELAY_PORT}|g" -e "s|\${SHARED_LINE_PORT}|${SHARED_LINE_PORT}|g" -e "s|\${RTPENGINE_NG_PORT}|${RTPENGINE_NG_PORT}|g" \
    -e "s|\${APP_MEDIA}|${APP_MEDIA}|g" -e "s|\${LOG_LEVEL}|${LOG_LEVEL}|g" -e "s|\${DBDIR}|${DBDIR}|g" \
    -e "s|\${USRLOC_DB_MODE}|${USRLOC_DB_MODE}|g" -e "s|\${PUBLIC_DOMAIN}|${PUBLIC_DOMAIN:-invalid}|g" \
    -e "s|\${EXT_NAME}|${EXT_NAME:-invalid}|g" -e "s|\${EXT_TCP_PORT}|${EXT_TCP_PORT}|g" -e "s|\${EXT_TLS_PORT}|${EXT_TLS_PORT}|g" \
    -e "s|\${EXT_ADV_TCP}|${EXT_ADV_TCP}|g" -e "s|\${EXT_ADV_TLS}|${EXT_ADV_TLS}|g" -e "s|\${CME_EXT_PORT}|${CME_EXT_PORT}|g" \
    -e "s|\${EXT_UDP_PORT}|${EXT_UDP_PORT}|g" -e "s|\${EXT_ADV_UDP}|${EXT_ADV_UDP}|g" \
    /usr/local/share/kamailio/kamailio.cfg.template > /run/kamailio/kamailio.cfg
# Zugang von außen einschalten (Präprozessor: #!ifdef WITH_EXT)
[ "$EXT" = 1 ] && sed -i '1a #!define WITH_EXT' /run/kamailio/kamailio.cfg
if grep -n '\${' /run/kamailio/kamailio.cfg; then
  echo "kamailio: Platzhalter nicht ersetzt (siehe oben)" >&2
  exit 1
fi
[ "$EXT" = 1 ] && echo "kamailio: von außen ${EXT_NAME} TCP ${EXT_TCP_PORT}/TLS ${EXT_TLS_PORT}/UDP ${EXT_UDP_PORT} (hier ${HOST_IP}), Adressen @${PUBLIC_DOMAIN}, zur CME über ${HOST_IP}:${CME_EXT_PORT}"
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
  if out=$(python3 /usr/local/bin/passwd2htable.py --aliases "$ALIASES" "$DBDIR" 2>/dev/null) && [ "$out" = "changed" ]; then
    kamcmd -s unix:/run/kamailio/kamailio_ctl htable.reload alias >/dev/null 2>&1 || true
    echo "kamailio: Adressen für Anrufe von draußen aktualisiert"
  fi
  # erneuertes Zertifikat (Traefik verlängert es selbst) ohne Neustart übernehmen
  if [ "$EXT" = 1 ] && out=$(cert_update 2>/dev/null) && [ "$out" = "changed" ]; then
    kamcmd -s unix:/run/kamailio/kamailio_ctl tls.reload >/dev/null 2>&1 || true
    echo "kamailio: TLS-Zertifikat erneuert"
  fi
done
wait $KPID
