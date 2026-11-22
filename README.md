# CME Phone – SIP-Softphone für Cisco CME (iPhone, iPad, Mac)

Eigenes Softphone für die Cisco-CME-Anlage (vcube, ISR 1100, IOS XE 17.12).
SwiftUI-Oberfläche + CallKit, SIP/RTP/Video über **liblinphone** (Linphone SDK).
Die **macOS-Version** telefoniert wahlweise selbst oder steuert ein Cisco-SIP-Tischtelefon
(siehe [macOS-Version](#macos-version)).

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

Belledonne bietet kein aktuelles fertiges Image an. Die Dockerfiles im Flexisip-Repo basieren noch
auf CentOS 7 bzw. Debian 9/10. Deshalb liegt ein eigenes Dockerfile in `server/flexisip/Dockerfile`.
Es baut Flexisip 2.6.2 auf Debian 13, nur mit dem Proxy-Teil. Per SSH auf docker01:

```bash
git clone https://gitea.wiederhol.de/thomas/cme-softphone.git /tmp/cme-softphone
docker build -t flexisip:local /tmp/cme-softphone/server/flexisip     # 20–40 Minuten
docker run --rm flexisip:local flexisip --version                    # Test
```

Ohne diesen Schritt baut Portainer das Image beim ersten Deploy selbst. Das kann aber in einen
Timeout der Weboberfläche laufen.

### 4. Stack auf docker01

```bash
sudo mkdir -p /opt/containers/cme-softphone/{flexisip,flexisip-log,apns}
sudo cp server/flexisip/flexisip.conf /opt/containers/cme-softphone/flexisip/
sudo sed -i 's/DOCKER01_IP/192.168.11.20/g; s/SIP_GW_NAME/sip-gw.wiederhol.de/g' \
     /opt/containers/cme-softphone/flexisip/flexisip.conf
sudo cp AuthKey_XXXX.p8 /opt/containers/cme-softphone/apns/AuthKey.p8
```

Danach in Portainer: **Stacks → Add stack → Repository**
* Repository-URL `https://gitea.wiederhol.de/thomas/cme-softphone` (mit Gitea-Zugangsdaten, das Repo ist privat)
* Compose path `server/docker-compose.yml`
* Umgebungsvariablen: `RELAY_LOCALPUSH_SECRET`, `APNS_KEY_ID`, `APNS_TEAM_ID`, optional `APNS_ENV`
* „Enable relative path volumes“ **aus** lassen, alle Pfade sind absolut

Portainer baut den push-relay dabei aus `server/push-relay/` selbst. Das Flexisip-Image muss vorher
wie in Schritt 3 auf docker01 gebaut sein.

**Traefik:** Flexisip und der Relay laufen bewusst im Host-Netz und **nicht hinter Traefik**. SIP
(UDP/TCP) und der Local-Push-Kanal sind keine HTTP-Dienste. Ein Proxy dazwischen würde die
Absender-IPs verstecken, und damit funktioniert Flexisips NAT- und Contact-Behandlung nicht mehr.
RTP läuft ohnehin direkt zwischen Gerät und CME. Für die SIP-Verbindung brauchst du also nur den
DNS-Eintrag und die Firewall-Regeln:

* DNS `sip-gw.wiederhol.de` → LAN-IP von docker01 (lokaler DNS in der UDM, kein Traefik-Router).
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

## macOS-Version

Eigenes Xcode-Projekt (`project-mac.yml`), gleicher SIP-Kern wie auf iOS. Die App läuft im Fenster
**und** in der Menüleiste weiter, wenn das Fenster zu ist.

### Zwei Betriebsarten (umschaltbar in Symbolleiste und Menüleiste)

| | Softphone | Tischtelefon steuern |
|---|---|---|
| Wer telefoniert | der Mac (Headset, Studio Display, Mac-Mikrofon) | dein Cisco 8851/8845/8865 |
| Wählen | SIP-INVITE vom Mac | `Dial:<nummer>` per CGI/Execute ans Telefon |
| Annehmen / Auflegen / Halten | SIP | Tasten-URIs (`Key:Speaker`, `Key:Hold` …, einstellbar) |
| DTMF | RFC 2833 | `SendDigits:<ziffern>:<leitung>` |
| Status / eingehende Anrufe | SIP-Anrufstatus | SIP-Statusabo (`SUBSCRIBE` dialog oder presence) auf die Nebenstelle des Telefons → Mitteilung mit „Annehmen“/„Ablehnen“ |
| Video | H.264 / H.265 | am Telefon (8845/8865) |

Außerdem in beiden Modi: `tel:`-, `sip:`- und `callto:`-Links (Kontakte, Safari, Mail) öffnen CME Phone
und wählen je nach Modus über den Mac oder über das Tischtelefon. Tastenkürzel: ⇧⌘A annehmen,
⇧⌘E auflegen (Tischtelefon). Audiogeräte sind in den Einstellungen wählbar.

### Warum nicht CSTA?

Cisco hat CTI/CSTA ab **Unified CME 12.6 (IOS XE 16.11.1a)** entfernt. Auf dem vcube (17.12) sind die
Befehle deaktiviert. Auch vorher ließen sich per CSTA nur **SCCP**-Telefone steuern, keine SIP-Telefone.
Die 88xx laufen bei dir als SIP. Der einzige Weg, sie an CME fernzusteuern, ist deshalb die
**Cisco IP Phone XML-Schnittstelle** (`CiscoIPPhoneExecute`). Die Steuerung steckt hinter dem
Protokoll `DeskPhoneBackend`. Falls du später auf **CUCM 15** gehst, kann dort ein JTAPI-/CTI-Backend
mit derselben Oberfläche dazukommen.

### Einrichtung

1. `cme/deskphone-control.cfg` anpassen und einspielen:
   * `url authentication` + `authentication credential cmephone <passwort>`, dann Telefon neu starten
   * Presence/BLF: `sip-ua presence enable`, `presence / allow subscribe / watcher all`, `allow watch` an der DN des Tischtelefons
   * eigener Pool für den Mac (z.B. Nst. 23)
2. Test im Terminal (muss am Telefon wählen):
   ```bash
   curl -u cmephone:<passwort> --data-urlencode 'XML=<CiscoIPPhoneExecute><ExecuteItem URL="Dial:23"/></CiscoIPPhoneExecute>' http://<telefon-ip>/CGI/Execute
   ```
   Antwort `CiscoIPPhoneError Number="4"` heißt: Authentifizierung schlägt fehl (URL/Credential/Neustart prüfen).
3. Projekt erzeugen:
   ```bash
   xcodegen generate --spec project-mac.yml
   open CMEPhoneMac.xcodeproj
   ```
   Das Swift-Paket für macOS ist `https://gitlab.linphone.org/BC/public/linphone-sdk-swift-macos` (Branch `stable`).
   Falls Xcode es nicht findet: in Xcode *File → Add Package Dependencies* die aktuelle URL aus dem
   Linphone-Wiki eintragen (Produkt `linphonesw`).
4. In der App unter *Einstellungen → Konto* die Mac-Nebenstelle (23) und CME eintragen, unter
   *Tischtelefon* IP, Nebenstelle (10), Benutzer/Passwort. Modus „Tischtelefon“ wählen.
5. Unter *Tischtelefon → Experte* die Tasten-URIs für dein Modell ausprobieren und unter
   *Einstellungen → Tischtelefon* zuordnen. Welche `Key:`-URIs ein 88xx annimmt, hängt von der Firmware ab.
   `Dial:` und `SendDigits:` funktionieren zuverlässig, Annehmen und Auflegen meist über `Key:Speaker`
   bzw. `Key:Headset` oder einen Softkey (`Key:Soft1` …).

### Tests (macOS)

| # | Test | Erwartung |
|---|---|---|
| M1 | Softphone: 23 → 10 | Mac telefoniert, Codec G.722 |
| M2 | Softphone: 10 → 23 | Mitteilung mit Annehmen/Ablehnen, Klingelton |
| M3 | Tischtelefon: Wählen 0711… | 8851 geht in Freisprechen und wählt |
| M4 | Tischtelefon: Anruf auf 10 | Status „Klingelt“ + Mitteilung, „Annehmen“ nimmt am Telefon ab |
| M5 | `tel:`-Link in Safari | wählt je nach Modus |
| M6 | Statusabo | `show presence subscription summary` zeigt das Abo von 23 auf 10 |

Wenn M4 keinen Status zeigt: in den Einstellungen das Event-Paket von `dialog` auf `presence` umstellen.
CME liefert je nach Konfiguration nur eines davon.

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
project.yml                 XcodeGen iOS/iPadOS (App + Local-Push-Extension)
project-mac.yml             XcodeGen macOS
CMEPhone/App/               iOS: App-Einstieg, AppDelegate
CMEPhone/Core/              gemeinsam iOS + macOS
  SIPManager.swift          liblinphone-Core: Konto, Codec-Reihenfolge, Anrufe, Video
  PhoneSettings.swift       Einstellungen (App-Group-UserDefaults), inkl. Mac/Tischtelefon
  CallKitProvider.swift     nur iOS: CallKit (als `CallCenter`)
  LocalPushController.swift nur iOS: NEAppPushManager
CMEPhone/UI/                iOS: Wählen, aktiver Anruf + Video, Einstellungen, Protokoll
CMEPhoneMac/App/            macOS: App, Menüleiste, tel:-Links, CallCenter (Mitteilungen)
CMEPhoneMac/DeskPhone/      Tischtelefon: CiscoXML (Execute), DialogInfo (Statusabo), DeskPhoneController
CMEPhoneMac/UI/             macOS: Hauptfenster, Wählen, Anruf + Video, Tischtelefon, Einstellungen
LocalPushProvider/          NEAppPushProvider (TCP zum Relay, reportIncomingCall)
Shared/                     gemeinsame Konstanten + Local-Push-Protokoll
server/flexisip/            flexisip.conf (Push-Gateway vor CME)
server/push-relay/          Python-Relay (Local Push + APNs HTTP/2, .p8-Token-Auth)
cme/softphone.cfg           IOS-Konfiguration für CME (Softphones)
cme/deskphone-control.cfg   IOS-Konfiguration für Tischtelefon-Steuerung + Mac-Pool
```

## Stand und offene Punkte

* **Kompiliert** mit Xcode 27.2 – macOS und iOS, bisher ohne Signierung: `zsh build.command`
  (Logs in `build-logs/`). Signiert bauen: in Xcode → Einstellungen → Accounts mit der Apple-ID
  anmelden, dann `SIGN=1 zsh build.command`. Noch nicht auf Geräten bzw. gegen CME getestet.
* Der push-relay ist getestet: Local-Push-Zustellung mit ACK, Secret-Prüfung, APNs-JWT, Rückfall auf APNs.
* Getestet (Swift unter Linux): Aufbau und Antwortauswertung von CiscoIPPhoneExecute, Auswertung von
  dialog-info- und PIDF-NOTIFYs (17 Fälle).
* Die CME-Befehle für `url authentication`/`authentication credential` und Presence sind gegen 17.12 zu prüfen.
* Tests ohne Xcode: `tests/run.sh` (Swift-Toolchain + Python 3 mit `aiohttp`, `httpx[http2]`, `PyJWT[crypto]`).
* Flexisip-Konfiguration und CME-Befehle (`video`/`camera` im SIP-Pool, `codec transparent`) müssen
  gegen deine Versionen geprüft werden.
* Passwort (falls genutzt) liegt in UserDefaults, ein Keychain-Umbau steht noch aus.
* Lizenz: Linphone SDK ist **AGPLv3**. Für privaten Einsatz problemlos, für eine App-Store-Veröffentlichung
  bräuchte es eine kommerzielle Lizenz von Belledonne.
