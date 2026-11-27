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
  -framework CoreFoundation -framework SystemConfiguration -framework Security -framework Foundation \
  -framework AVFoundation -framework VideoToolbox -framework CoreMedia -framework CoreVideo -lz -lresolv -o $T/test_call
($T/test_call B 15070 > $T/b.txt 2>&1 &)
sleep 1
RECFILE=$T/rec.wav $T/test_call A 15080 "sip:B@$IP:15070" > $T/a.txt 2>&1
sleep 1.5
pkill -f "$T/test_call" || true
ok=$(grep -c 'SIP/2.0 200' $T/a.txt); ci=$(grep -c 'Call-Info' $T/a.txt)
rec=$(stat -f %z $T/rec.wav 2>/dev/null || echo 0)
echo "200 OK: $ok (erwartet 4), Call-Info: $ci (erwartet 2), $(grep -o 'info: .*' $T/a.txt), Aufnahme: $rec Byte (erwartet > 50000), $(grep -o 'record -*[0-9]*' $T/a.txt)"
[ "$ok" -ge 4 ] && [ "$ci" -eq 2 ] && grep -q "menc=srtp" $T/a.txt && [ "$rec" -gt 50000 ] || exit 1

# Zweiter Lauf über TLS: B mit selbst signiertem Zertifikat (IP im SAN), A prüft es (sip_cafile)
openssl req -x509 -newkey rsa:2048 -nodes -keyout $T/key.pem -out $T/crt.pem -days 1 -subj "/CN=$IP" \
  -addext "subjectAltName=IP:$IP" >/dev/null 2>&1
cat $T/crt.pem $T/key.pem > $T/b.pem
(CERT=$T/b.pem $T/test_call B 15090 > $T/b2.txt 2>&1 &)
sleep 1
CAFILE=$T/crt.pem $T/test_call A 15100 "sip:B@$IP:15091;transport=tls" > $T/a2.txt 2>&1
sleep 1.5
pkill -f "$T/test_call" || true
ok=$(grep -c 'SIP/2.0 200' $T/a2.txt)
echo "TLS: 200 OK: $ok (erwartet 4), $(grep -o 'info: .*' $T/a2.txt)"
[ "$ok" -ge 4 ] && grep -q "menc=srtp" $T/a2.txt


# Video: H.264 und H.265 mit Testbild in beide Richtungen
for vc in H264 H265; do
  (VCODEC=$vc $T/test_call B 15130 > $T/bv.txt 2>&1 &)
  sleep 1
  LOGALL=1 VCODEC=$vc $T/test_call A 15140 "sip:B@$IP:15130" > $T/av.txt 2>&1
  sleep 1.5
  pkill -f "$T/test_call" || true
  ra=$(grep -o 'A video remote=[0-9]*' $T/av.txt | grep -o '[0-9]*$' || echo 0)
  rb=$(grep -o 'B video remote=[0-9]*' $T/bv.txt | grep -o '[0-9]*$' || echo 0)
  echo "Video $vc: Bilder bei A $ra, bei B $rb (erwartet je > 10), $(grep -ho 'Kodierer [^,]*' $T/av.txt | head -1)"
  [ "$ra" -gt 10 ] && [ "$rb" -gt 10 ] || { grep -h -i -E "cmevideo|video" $T/av.txt $T/bv.txt | head -20; exit 1; }
done
