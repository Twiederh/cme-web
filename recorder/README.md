# cme-recorder – Gesprächsaufzeichnung für CUBE / Cisco CME

Der Router kopiert die Sprache externer Gespräche (Media Forking, SIPREC) und schickt sie an diesen
Container. Je Gespräch entsteht eine Stereo-MP3 (links die interne Seite, rechts die Gegenseite) mit
JSON-Metadaten, optional mit Abschrift (Whisper) und Mail. Eingerichtet und bedient wird alles in
cme-web im Tab **Aufzeichnung**.

> In Deutschland dürfen Gespräche nur mit Einwilligung aller Beteiligten aufgezeichnet werden (§ 201 StGB).

## Ports auf dem Docker-Host

| Port | Wofür |
|---|---|
| UDP/TCP 5070 | SIP vom Router (nur von `CME_HOST` angenommen) |
| UDP 10100–10139 | Sprache (2 Ports je Gespräch, also 20 Gespräche gleichzeitig) |

## Was cme-web am Router einrichtet

```
voice class codec 8999                     ! Codecs, die der Recorder versteht
 codec preference 1 g711alaw
 codec preference 2 g711ulaw
 codec preference 3 g722-64
voice class uri 8999 sip                   ! Anrufe von Asterisk erkennen
 host ipv4:<VM_HOST_IP>
dial-peer voice 8190 voip                  ! zum Recorder
 description Aufzeichnung (cme-recorder)
 destination-pattern 89990001
 session protocol sipv2
 session target ipv4:<VM_HOST_IP>:5070
 session transport udp
 voice-class codec 8999
dial-peer voice 8191 voip                  ! Anrufe von Asterisk (IVR, Ansage) nicht doppelt aufzeichnen
 description Von Asterisk (cme-web)
 incoming uri via 8999
dial-peer voice 8192 voip                  ! nur mit Ansage: <prefix>T -> Asterisk
 description Aufzeichnungshinweis (Asterisk)
 destination-pattern 8199T
 session target ipv4:<VM_HOST_IP>:5060
media profile recorder 8999
 media-type audio
 media-recording 8190
media class 8999
 recorder profile 8999 siprec
dial-peer voice 2 voip                     ! die ausgewählten Dial-Peers zum Provider
 media-class 8999
```

Die Nummern (Tags) können abweichen, wenn sie schon belegt sind.

## Ansage vor eingehenden Gesprächen

Damit Anrufer den Hinweis hören, muss der eingehende Anruf erst zu Asterisk. Dazu die
Übersetzungsregel für eingehende Anrufe so ändern, dass vor die interne Nummer `8199` gesetzt wird
(z. B. `/^\+49301234567\(..\)$/` → `/8199\1/`). Der Router schickt `8199…` an Asterisk, Asterisk spielt
die Ansage als Early Media (der Anruf ist noch nicht angenommen) und ruft dann die Nummer dahinter an.

## Prüfen

```
show call active voice brief          ! je aufgezeichnetem Gespräch ein zusätzlicher Abschnitt zum Recorder
show voip recmsp session              ! laufende Aufzeichnungen
debug ccsip messages                  ! INVITE an <VM_HOST_IP>:5070 mit multipart/mixed (SDP + Metadaten)
```

Im Tab Aufzeichnung zeigt cme-web unten das Protokoll des Recorders.

## Dateien

- `recordings`-Volume: `<id>.mp3`, `<id>.json`, `.status.json` (Lebenszeichen für cme-web)
- Einstellungen: `cme-recording.json` im Volume `vm-config` (schreibt cme-web)
