# Medien-Anker für die Apps (Kamailio steuert ihn über das ng-Protokoll auf 127.0.0.1):
# SRTP (SDES) zu den iOS-Geräten, RTP zur CME. Nur Userspace-Weiterleitung, kein Kernelmodul.
FROM debian:trixie

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends rtpengine-daemon \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/rtpengine-entrypoint
RUN chmod +x /usr/local/bin/rtpengine-entrypoint

ENTRYPOINT ["/usr/local/bin/rtpengine-entrypoint"]
