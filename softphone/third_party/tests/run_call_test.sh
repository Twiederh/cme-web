#!/bin/zsh
# Anruftest der C-Schicht: zwei Prozesse direkt über die LAN-Adresse (baresip nimmt kein Loopback).
# A ruft B mit SRTP (RTP/SAVP wie Kamailio zu iOS), hält mit Call-Info: remotecc, setzt fort, legt auf.
set -eu
cd "${0:A:h}"
IP=$(ipconfig getifaddr en0)
B=../build
T=$(mktemp -d)
sed "s|LAN_IP|$IP|g" test_call.c > $T/test_call.c
xcrun clang -I$B/headers $T/test_call.c $B/macos/libcmesip.a -framework CoreAudio -framework AudioToolbox \
  -framework CoreFoundation -framework SystemConfiguration -framework Security -lz -lresolv -o $T/test_call
($T/test_call B 15070 > $T/b.txt 2>&1 &)
sleep 1
$T/test_call A 15071 "sip:B@$IP:15070" > $T/a.txt 2>&1
sleep 1.5
pkill -f "$T/test_call" || true
ok=$(grep -c 'SIP/2.0 200' $T/a.txt); ci=$(grep -c 'Call-Info' $T/a.txt)
echo "200 OK: $ok (erwartet 4), Call-Info: $ci (erwartet 2), $(grep -o 'info: .*' $T/a.txt)"
[ "$ok" -ge 4 ] && [ "$ci" -eq 2 ] && grep -q "menc=srtp" $T/a.txt
