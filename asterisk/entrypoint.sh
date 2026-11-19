#!/bin/bash
# Startet Asterisk als Voicemail fuer CME. Konfiguration aus Vorlagen + Umgebungsvariablen.
set -euo pipefail

missing=""
[ -n "${HOST_IP:-}" ] || missing="$missing VM_HOST_IP"
[ -n "${CME_IP:-}" ] || missing="$missing CME_HOST"
[ -n "${AMI_SECRET:-}" ] || missing="$missing VM_AMI_SECRET"
if [ -n "$missing" ]; then
  # Nicht neu starten lassen, sondern warten, bis die Variablen gesetzt sind
  echo "Voicemail nicht eingerichtet, es fehlt:$missing (im Stack setzen und neu deployen)"
  exec sleep infinity
fi
# Neuere Docker-Versionen geben Containern ein riesiges Datei-Limit (~1 Mrd.).
# Asterisk bleibt damit beim Start nach den Codecs haengen -> Limit senken.
ulimit -n "${ASTERISK_NOFILE:-65536}" 2>/dev/null || true
RTP_START="${RTP_START:-10000}"
RTP_END="${RTP_END:-10049}"
# Eigenes Docker-Netz: dort spricht Asterisk direkt, sonst mit HOST_IP
LOCAL_NET="${LOCAL_NET:-$( (ip -o -4 route show scope link 2>/dev/null || true) | awk 'NR==1 {print $1}')}"
LOCAL_NET="${LOCAL_NET:-172.16.0.0/12}"

mkdir -p /etc/asterisk/cme
for f in pjsip_mwi.conf mailboxes.conf vm_general.conf globals.conf; do
  [ -e "/etc/asterisk/cme/$f" ] || : > "/etc/asterisk/cme/$f"
done
[ -e /etc/asterisk/cme/extensions_cme.conf ] || echo "[cme-numbers]" > /etc/asterisk/cme/extensions_cme.conf
[ -e /etc/asterisk/cme/extensions_rec.conf ] || echo "[cme-recnotice]" > /etc/asterisk/cme/extensions_rec.conf
[ -e /etc/asterisk/cme/extensions_queue.conf ] || printf '[cme-queues]\n[queue-run]\n' > /etc/asterisk/cme/extensions_queue.conf
for f in queues.conf musiconhold.conf; do
  [ -e "/etc/asterisk/cme/$f" ] || : > "/etc/asterisk/cme/$f"
done
mkdir -p /etc/asterisk/cme/sounds /etc/asterisk/cme/moh /var/spool/asterisk/voicemail/fax

for src in /opt/cme-asterisk/*.conf; do
  sed -e "s|@HOST_IP@|${HOST_IP}|g" -e "s|@CME_IP@|${CME_IP}|g" \
      -e "s|@AMI_SECRET@|${AMI_SECRET}|g" -e "s|@LOCAL_NET@|${LOCAL_NET}|g" \
      -e "s|@RTP_START@|${RTP_START}|g" -e "s|@RTP_END@|${RTP_END}|g" \
      "$src" > "/etc/asterisk/$(basename "$src")"
  chmod 640 "/etc/asterisk/$(basename "$src")"
done

mkdir -p /var/spool/asterisk/voicemail/default /run/asterisk
chown -R asterisk:asterisk /etc/asterisk /var/spool/asterisk /var/lib/asterisk /var/log/asterisk /run/asterisk
chmod -R g+rwX,u+rwX /etc/asterisk/cme /var/spool/asterisk/voicemail

echo "Asterisk-Voicemail: HOST_IP=${HOST_IP} CME_IP=${CME_IP} LOCAL_NET=${LOCAL_NET} RTP ${RTP_START}-${RTP_END}"
exec asterisk -f -U asterisk -G asterisk -vv
