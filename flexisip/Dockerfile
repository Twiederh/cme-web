# Flexisip als SIP-Push-Gateway – gebaut aus den offiziellen Quellen.
# (Die Dockerfiles im Flexisip-Repo basieren noch auf CentOS 7 / Debian 9/10 und sind veraltet.)
#
# Bauen (einmalig auf docker01, dauert je nach CPU 20–40 Minuten):
#   docker build -t flexisip:local server/flexisip
# oder automatisch beim ersten Deploy des Portainer-Stacks.
#
# Proxy und B2BUA. Der B2BUA (liblinphone) steht zwischen den Apps und CME: CME sieht ihn als angemeldetes
# Telefon (Contact = seine Adresse = Contact des Pools MOB<Nummer>) - nur so gehoeren Anrufe der iPhones zur
# Shared Line (Halten sichtbar, Uebernehmen in beide Richtungen). Der B2BUA braucht SOCI (ohne Datenbank-Backends).
# RegEvent bleibt an: nur dann baut Flexisip 2.6.2 belle-sip mit, das liblinphone braucht (cmake/LinphoneSDK.cmake).
# Presence, Konferenz, Voicemail, Transcoder und SNMP sind abgeschaltet (Redis gebaut, aber nicht genutzt).

FROM debian:trixie

ARG FLEXISIP_VERSION=2.6.2
# Linphone-SDK-Stand, den Flexisip 2.6.2 als Submodul einbindet (Tag 5.5.16).
# Muss vorgegeben werden: der flache Klon hat keine Git-Tags, "git describe" schlägt sonst fehl.
# Bei einem Flexisip-Update anpassen: git ls-remote --tags https://gitlab.linphone.org/BC/public/linphone-sdk.git | grep <submodul-sha>
ARG LINPHONESDK_VERSION=5.5.16
ARG NJOBS=4

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git g++ cmake ninja-build make pkg-config doxygen \
      python3 python3-pystache python3-six yasm \
      libssl-dev libboost-dev libboost-system-dev libboost-thread-dev \
      libcpp-jwt-dev libjansson-dev libjsoncpp-dev libnghttp2-dev libsqlite3-dev \
      libxerces-c-dev libsrtp2-dev libgsm1-dev libopus-dev libmbedtls-dev \
      libspeex-dev libspeexdsp-dev libxml2-dev libhiredis-dev libsystemd-dev libvpx-dev \
      zlib1g-dev default-libmysqlclient-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${FLEXISIP_VERSION}" \
      --recurse-submodules --shallow-submodules -j 8 \
      https://gitlab.linphone.org/BC/public/flexisip.git flexisip

# Cisco-CME-Anpassung des B2BUA (Halten mit Call-Info, re-INVITE ohne Replaces), siehe Skript
COPY patch-b2bua-cisco.py /src/patch-b2bua-cisco.py
RUN python3 /src/patch-b2bua-cisco.py /src/flexisip

RUN cmake -S flexisip -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCMAKE_INSTALL_PREFIX=/opt/belledonne-communications \
      -DSYSCONF_INSTALL_DIR=/etc \
      -DFLEXISIP_VERSION="${FLEXISIP_VERSION}" \
      -DLINPHONESDK_VERSION="${LINPHONESDK_VERSION}" \
      -DLINPHONESDK_STATE=releases \
      -DLINPHONESDK_BRANCH=release \
      -DENABLE_STRICT=OFF \
      -DENABLE_SANITIZERS=OFF \
      -DENABLE_SYSTEMD=OFF \
      -DENABLE_PRESENCE=OFF \
      # Redis an: der RegEvent-Code verweist auf die Redis-Client-Klassen (ohne: Linkfehler in bin/flexisip)
      -DENABLE_REDIS=ON \
      -DENABLE_SNMP=OFF \
      -DENABLE_SOCI=ON \
      -DENABLE_SOCI_POSTGRESQL_BACKEND=OFF \
      -DENABLE_REGEVENT=ON \
      -DENABLE_B2BUA=ON \
      -DENABLE_TRANSCODER=OFF \
      -DENABLE_VOICEMAIL=OFF \
      -DENABLE_OPENID_CONNECT=OFF \
      -DENABLE_EXTERNAL_AUTH_PLUGIN=OFF \
      -DENABLE_UNIT_TESTS=OFF \
 && cmake --build build -j "${NJOBS}" \
 && cmake --install build \
 && rm -rf /src

ENV PATH=/opt/belledonne-communications/bin:$PATH
RUN mkdir -p /etc/flexisip /var/opt/belledonne-communications/log/flexisip \
 && flexisip --version

COPY flexisip.conf.template /usr/local/share/flexisip/flexisip.conf.template
COPY entrypoint.sh /usr/local/bin/flexisip-entrypoint
RUN chmod 755 /usr/local/bin/flexisip-entrypoint
# /etc/flexisip: optional eigene flexisip.local.conf (ersetzt die Vorlage)
VOLUME ["/etc/flexisip", "/var/opt/belledonne-communications/log/flexisip"]
ENTRYPOINT ["/usr/local/bin/flexisip-entrypoint"]
