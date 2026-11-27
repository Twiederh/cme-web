# SIP-Server für die Apps (ersetzt Flexisip): Registrar, Proxy zur CME, Push über cme-push-relay.
# Medien (SRTP zu den iOS-Geräten, RTP zur CME) laufen über rtpengine (eigener Container, rtpengine/).
FROM debian:trixie

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      kamailio kamailio-extra-modules kamailio-utils-modules kamailio-json-modules kamailio-tls-modules \
      ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*

COPY kamailio.cfg.template /usr/local/share/kamailio/kamailio.cfg.template
COPY passwd2htable.py /usr/local/bin/passwd2htable.py
COPY acme2pem.py /usr/local/bin/acme2pem.py
COPY entrypoint.sh /usr/local/bin/kamailio-entrypoint
RUN chmod +x /usr/local/bin/kamailio-entrypoint

ENTRYPOINT ["/usr/local/bin/kamailio-entrypoint"]
