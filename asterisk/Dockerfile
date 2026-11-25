# Asterisk nur als Voicemail-Server fuer Cisco CME (Ansagen deutsch)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive TZ=Europe/Berlin
# Benutzer asterisk bekommt die UID von cme-web (10001), damit beide die Volumes teilen koennen
RUN apt-get update \
 && apt-get install -y --no-install-recommends asterisk asterisk-modules asterisk-core-sounds-en \
      asterisk-prompt-de iproute2 tzdata python3 ca-certificates libtiff-tools \
 && rm -rf /var/lib/apt/lists/* \
 && OLD_UID=$(id -u asterisk) && OLD_GID=$(id -g asterisk) \
 && groupmod -g 10001 asterisk && usermod -u 10001 -g 10001 asterisk \
 && { find / -xdev \( -uid "$OLD_UID" -o -gid "$OLD_GID" \) -exec chown -h asterisk:asterisk {} + || true; } \
 && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk

# Deutsche Ansagen von asterisksounds.org (2.11.19, 16 kHz, CC BY-SA 3.0 - LICENSE.txt liegt im Ordner).
# Die Ansagen aus asterisk-prompt-de sind 8-kHz-GSM und zu 80 % uebersteuert: mit G.722 (CME, sipgate) hoert man
# das als Knacksen. Die Domain asterisksounds.org gehoert nicht mehr dem Projekt - deshalb die Originaldateien
# aus dem Internet Archive, geprueft gegen die SHA-256 aus dem OpenEmbedded-Rezept asterisk-sounds-de.
# Gleichnamige Dateien aus asterisk-prompt-de werden entfernt (sonst nimmt Asterisk fuer a-law/u-law evtl. GSM);
# was im neuen Satz fehlt, bleibt als Rueckfall erhalten.
ARG SOUNDS_DE=https://web.archive.org/web/2021052209245
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip && rm -rf /var/lib/apt/lists/* \
 && cd /tmp \
 && curl -fsSL --retry 5 --retry-delay 10 -o core.zip \
      "${SOUNDS_DE}5id_/https://www.asterisksounds.org/sites/asterisksounds.org/files/sounds/de/download/asterisk-sounds-core-de-2.11.19.zip" \
 && curl -fsSL --retry 5 --retry-delay 10 -o extra.zip \
      "${SOUNDS_DE}6id_/https://www.asterisksounds.org/sites/asterisksounds.org/files/sounds/de/download/asterisk-sounds-extra-de-2.11.19.zip" \
 && printf '%s  %s\n' cbdef15430478270ff67f0f18ca7123630975df8953be3d650531bc90a5a14b6 core.zip \
      2a416b1d8d1b661d1aab29b020280f181b880909a06c30769612e96423f39aa7 extra.zip | sha256sum -c - \
 && S=/usr/share/asterisk/sounds/de \
 && unzip -qo core.zip -d "$S" && unzip -qo extra.zip -d "$S" && rm -f core.zip extra.zip \
 && find "$S" -name '*.sln16' | while read -r f; do b="${f%.sln16}"; rm -f "$b.gsm" "$b.wav" "$b.alaw" "$b.ulaw" "$b.g722"; done \
 && chown -R root:root "$S" && chmod -R a+rX "$S" \
 && apt-get purge -y unzip && apt-get autoremove -y

COPY etc/ /opt/cme-asterisk/
COPY entrypoint.sh /entrypoint.sh
COPY vm-mail.py fax-mail.py /usr/local/bin/
RUN chmod 755 /usr/local/bin/vm-mail.py /usr/local/bin/fax-mail.py

EXPOSE 5060/udp 10000-10049/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD asterisk -rx "core show uptime" >/dev/null || exit 1
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
