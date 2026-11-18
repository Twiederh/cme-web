# Asterisk nur als Voicemail-Server fuer Cisco CME (Ansagen deutsch)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Berlin
# Benutzer asterisk bekommt die UID von cme-web (10001), damit beide die Volumes teilen koennen
RUN apt-get update \
 && apt-get install -y --no-install-recommends asterisk asterisk-modules asterisk-core-sounds-en \
      asterisk-prompt-de iproute2 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && OLD_UID=$(id -u asterisk) && OLD_GID=$(id -g asterisk) \
 && groupmod -g 10001 asterisk && usermod -u 10001 -g 10001 asterisk \
 && { find / -xdev \( -uid "$OLD_UID" -o -gid "$OLD_GID" \) -exec chown -h asterisk:asterisk {} + || true; } \
 && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk

COPY etc/ /opt/cme-asterisk/
COPY entrypoint.sh /entrypoint.sh

EXPOSE 5060/udp 10000-10049/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD asterisk -rx "core show uptime" >/dev/null || exit 1
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
