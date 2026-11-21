# CME Phone – SIP-Softphone für Cisco CME mit CallKit (iPhone & iPad)

Eigenes Softphone für die Cisco-CME-Anlage (vcube, ISR 1100, IOS XE 17.12).
SwiftUI-Oberfläche + CallKit, SIP/RTP/Video über **liblinphone** (Linphone SDK).

| | |
|---|---|
| Audio | G.711 A-law/µ-law, G.722, Opus (Priorität in den Einstellungen per Drag & Drop) |
| Video | H.264, H.265/HEVC (Hardware-Codec über VideoToolbox) |
| DTMF | RFC 2833 (`dtmf-relay rtp-nte`) |
| System | CallKit (Sperrbildschirm, Bluetooth, CarPlay, „Zuletzt“), PushKit, Local Push |
| Funktionen | Wählen, Annehmen/Ablehnen, Halten, Stumm, Lautsprecher, Tastenfeld, Weiterleiten (REFER), Video an/aus, Kamerawechsel, Protokoll |

---

## Architektur

```
                         Heim-WLAN: Local Push (TCP 5070, ohne Internet)
                 ┌─────────────────────────────────────────────────────┐
                 │                                                     ▼
┌──────────────┐ │  SIP (TCP 5060)   ┌──────────────┐   SIP    ┌──────────────────┐
│ iPhone/iPad  │─┴──────────────────▶│  Flexisip     │────────▶│ CME 192.168.11.4  │──▶ Giganetz / sipgate
│ CME Phone    │◀────────────────────│  Push-Gateway │◀────────│ (vcube)           │──▶ 88xx, Gigaset, ATA
│ +LocalPush-  │                     │  (docker01)   │         └──────────────────┘
│  Extension   │◀── APNs VoIP-Push ──│ cme-push-relay│
└──────────────┘    (unterwegs)      └──────────────┘
        ▲  RTP/SRTP direkt (LAN oder VPN) ───────────────────────────▶ CME / Telefone
```

**Warum ein Gateway?** iOS hält im Hintergrund keine SIP-Verbindung offen. Eingehende Anrufe müssen
per Push gemeldet werden, aber CME kann keine Pushes senden. Flexisip sitzt deshalb als
„Push-Gateway“ vor CME:

1. Die App registriert `sip:21@192.168.11.4` über Flexisip (Outbound-Proxy). Flexisip reicht das REGISTER
   an CME weiter, setzt dabei seine eigene Adresse als Contact und speichert die Push-Parameter (RFC 8599),
   die liblinphone automatisch mitsendet.
2. Ruft jemand die 21 an, schickt CME das INVITE an Flexisip. Schläft die App, ruft Flexisip den
   **cme-push-relay** auf:
   * Gerät im Heim-WLAN (Local-Push-Extension verbunden) → Meldung direkt über TCP.
   * sonst → APNs-VoIP-Push.
3. iOS weckt die App, die App meldet den Anruf **sofort** an CallKit, registriert sich neu, und Flexisip
   stellt das INVITE zu (`fork-late`).

Unterwegs brauchst du eine Verbindung ins Heimnetz (UniFi-VPN/WireGuard), denn RTP läuft direkt.
Ohne VPN müsstest du in Flexisip `MediaRelay` aktivieren und SIP/RTP in der UDM freigeben (nicht empfohlen).

---

## Was CME bei den Codecs mitmacht

| Codec | App ↔ Cisco 88xx / Gigaset / Amt | App ↔ App |
|---|---|---|
| G.711 | ✅ | ✅ |
| G.722 | ✅ (88xx, Gigaset N610 – je nach Konfiguration) | ✅ |
| Opus | ❌ CME/ISR 1100 kann nicht umkodieren | ⚠️ nur wenn CME das SDP durchreicht (`codec transparent`, Experiment in `cme/softphone.cfg` §5) |
| H.264 | ✅ mit 8845/8865 (Video-Durchleitung) | ✅ |
| H.265 | ❌ (88xx können kein HEVC) | ⚠️ wie Opus |

Die App bietet alle aktivierten Codecs in der eingestellten Reihenfolge an. Die Gegenstelle wählt,
es gibt also keine Fehlschläge, nur ggf. einen „kleineren“ Codec. Der tatsächlich genutzte Codec
steht während des Gesprächs unter dem Namen.

---

## Einrichtung

### 1. Apple Developer Portal

1. **Identifiers → App IDs**
   * `de.wiederhol.cmephone`: Capabilities *Push Notifications*, *App Groups*, *Network Extensions*
   * `de.wiederhol.cmephone.localpush`: *App Groups*, *Network Extensions*
2. **App Group** `group.de.wiederhol.cmephone` anlegen und beiden IDs zuweisen.
3. **Keys → +** → *Apple Push Notifications service (APNs)* → `.p8` herunterladen. Key-ID und Team-ID notieren.
4. Local Push (`app-push-provider`) ist Teil der Network-Extension-Capability. Falls Xcode beim Signieren
   meckert, muss das Entitlement bei Apple beantragt werden (Formular „Local Push Connectivity“).
   Bis dahin funktioniert APNs allein.

### 2. Xcode-Projekt erzeugen

```bash
brew install xcodegen
git clone https://gitea.wiederhol.de/thomas/cme-softphone.git
cd cme-softphone
# Team-ID in project.yml unter DEVELOPMENT_TEAM eintragen
xcodegen generate
open CMEPhone.xcodeproj
```

Xcode lädt das Linphone-SDK über Swift Package Manager (Branch `stable` mit Video, ca. 300 MB).
Auf einem echten Gerät bauen, denn im Simulator gibt es weder PushKit noch Local Push.

### 3. Flexisip-Image bauen (einmalig, auf docker01)

Belledonne bietet kein offizielles Image auf Docker Hub an, das Dockerfile liegt aber im Repo:

```bash
git clone --recursive https://gitlab.linphone.org/BC/public/flexisip.git
cd flexisip
docker build -t flexisip:local --build-arg njobs=8 -f docker/flex-from-src .
```

Alternativ ein Community-Image nutzen (`FLEXISIP_IMAGE=etnperlong/flexisip:latest` in `.env`, Pfade prüfen).

### 4. Stack auf docker01

```bash
sudo mkdir -p /opt/containers/cme-softphone/{flexisip,flexisip-log,apns}
sudo cp server/flexisip/flexisip.conf /opt/containers/cme-softphone/flexisip/
sudo sed -i 's/DOCKER01_IP/192.168.11.20/g; s/SIP_GW_NAME/sip-gw.wiederhol.de/g' \
     /opt/containers/cme-softphone/flexisip/flexisip.conf
sudo cp AuthKey_XXXX.p8 /opt/containers/cme-softphone/apns/AuthKey.p8
cp server/.env.example server/.env   # Secret, Key-ID, Team-ID eintragen
```

Danach in Portainer einen Stack aus `server/docker-compose.yml` anlegen (Build-Kontext = `server/`)
und die Werte aus `.env` als Umgebungsvariablen eintragen.

* DNS `sip-gw.wiederhol.de` → LAN-IP von docker01 (lokaler DNS in der UDM).
* Firewall: aus WLAN/VPN zu docker01 **TCP/UDP 5060** und **TCP 5070** erlauben.
  Port 8090 bleibt lokal (127.0.0.1).
* Test: `curl http://127.0.0.1:8090/health` auf docker01.

### 5. CME konfigurieren

`cme/softphone.cfg` anpassen (IP von docker01, Nebenstellen) und einspielen. Die Softphones werden
über **`id network <Flexisip-IP>`** zugeordnet, also ohne MAC-Adresse und ohne Passwort, wie bei deinen
anderen Geräten. Optional kannst du die Pools auch in **cme-web** anlegen.

### 6. App einrichten

Einstellungen in der App:

* Nebenstelle `21`, CME-Adresse `192.168.11.4`
* Push-Gateway an, Host `sip-gw.wiederhol.de`, Port 5060, TCP
* Local Push an, SSIDs deines WLANs, Relay `sip-gw.wiederhol.de:5070`, Secret wie im Stack
* **Übernehmen und neu anmelden**, danach muss der Status „Angemeldet“ zeigen.

---

## Testplan

| # | Test | Erwartung | Wenn nicht |
|---|---|---|---|
| 1 | App offen, Status | „Angemeldet“; `show voice register pool 21` zeigt Registrierung | Flexisip-Log: kommt das REGISTER an? `id network` = Flexisip-IP? |
| 2 | 21 → 10 (Tischtelefon) | Verbindung, Codec G.722 | Protokoll-Tab ansehen, `debug ccsip messages` |
| 3 | 10 → 21, App im Vordergrund | Klingelt per CallKit | CME muss die `CtRt…;doroute`-Parameter im Contact behalten, prüfen mit `debug ccsip messages` |
| 4 | 10 → 21, App beendet, im WLAN | Klingelt über Local Push | `curl :8090/devices` zeigt Gerät? Relay-Log |
| 5 | wie 4, mobil + VPN | Klingelt über APNs | Relay-Log: APNs-Status; `APNS_ENV=sandbox` für Debug-Builds |
| 6 | Video 21 → 8865 | H.264-Video | `voice-class codec 21` mit `video codec h264` am Pool |
| 7 | iPhone ↔ iPad | Opus + H.265, falls CME durchreicht | Experiment §5 in `softphone.cfg` |
| 8 | Extern (sipgate) | G.711 A-law | Dial-Peer-Codecs des Trunks |

**Fallback**, falls Test 3 scheitert (CME verwirft die URI-Parameter des Contacts): Flexisip als
eigenständigen Registrar betreiben (`reg-on-response=false`, Router ohne Filter) und in CME einen
Dial-Peer `destination-pattern 2[12]` → `session target ipv4:<docker01>`. Die Softphones sind dann in
CME keine registrierten Telefone, sondern hängen an einem SIP-Trunk.

---

## Projektstruktur

```
project.yml                 XcodeGen-Definition (App + Extension, SPM linphonesw)
CMEPhone/App/               App-Einstieg, AppDelegate
CMEPhone/Core/
  SIPManager.swift          liblinphone-Core: Konto, Codec-Reihenfolge, Anrufe, Video
  CallKitProvider.swift     CXProvider/CXCallController, Call-ID ↔ UUID, Audio-Session
  LocalPushController.swift NEAppPushManager-Konfiguration + Delegate
  PhoneSettings.swift       Einstellungen (App-Group-UserDefaults)
CMEPhone/UI/                SwiftUI: Wählen, aktiver Anruf + Video, Einstellungen, Protokoll
LocalPushProvider/          NEAppPushProvider (TCP zum Relay, reportIncomingCall)
Shared/                     gemeinsame Konstanten + Local-Push-Protokoll
server/flexisip/            flexisip.conf (Push-Gateway vor CME)
server/push-relay/          Python-Relay (Local Push + APNs HTTP/2, .p8-Token-Auth)
cme/softphone.cfg           IOS-Konfiguration für CME
```

## Stand und offene Punkte

* **Nicht kompiliert.** Der Code wurde ohne Xcode geschrieben. Die liblinphone-Swift-API ist
  nach Doku und Tutorials umgesetzt, kleine Signaturabweichungen der aktuellen SDK-Version
  (z.B. `activateAudioSession(actived:)`, `setVideodevice`) korrigiert Xcode bzw. der Claude Agent in Xcode schnell.
* Der push-relay ist getestet: Local-Push-Zustellung mit ACK, Secret-Prüfung, APNs-JWT, Rückfall auf APNs.
* Flexisip-Konfiguration und CME-Befehle (`video`/`camera` im SIP-Pool, `codec transparent`) müssen
  gegen deine Versionen geprüft werden.
* Passwort (falls genutzt) liegt in UserDefaults, ein Keychain-Umbau steht noch aus.
* Lizenz: Linphone SDK ist **AGPLv3**. Für privaten Einsatz problemlos, für eine App-Store-Veröffentlichung
  bräuchte es eine kommerzielle Lizenz von Belledonne.
