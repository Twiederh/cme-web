# CME Phone – Softphone für Cisco CME (iPhone, iPad, Mac)

Teil von cme-web. SIP/RTP über **liblinphone** (Linphone SDK), Server-Teil im gemeinsamen Stack
(`../flexisip`, `../push-relay`), Verwaltung in cme-web im Tab **Softphone**.

| | iPhone / iPad | Mac |
|---|---|---|
| Oberfläche | keine eigene – Telefon-App von iOS (CallKit) | nur Menüleiste + Call-Toast unten rechts |
| Anrufen | „Zuletzt“, Kontakte, Siri, `tel:`-Links (als Standard-App für Anrufe) | Wähltastatur, Anrufliste, `tel:`-Links |
| Eingehend | Sperrbildschirm/CallKit, auch wenn die App schläft (Local Push / APNs) | Call-Toast: Voicemail · Ablehnen · Annehmen |
| Im Gespräch | CallKit; App-Symbol im Anrufbildschirm öffnet den Aufnahme-Knopf | Toast: Aufnahme · Auflegen |
| Einstellungen | Einstellungen-App → Apps → CME Phone | Menüleiste → Einstellungen |
| Audio | G.722, G.711 A/µ, Opus | wie iOS |
| Video | H.264/H.265 (Gegenstelle 8845/8865) | – |

## Architektur

```
                         Heim-WLAN: Local Push (TCP 5072, ohne Internet)
                 ┌─────────────────────────────────────────────────────┐
                 │                                                     ▼
┌──────────────┐ │  SIP (TCP 5062)   ┌──────────────┐   SIP    ┌──────────────────┐
│ iPhone/iPad/ │─┴──────────────────▶│ cme-flexisip │────────▶│ CME (vcube)       │──▶ Amt
│ Mac          │◀────────────────────│ Push-Gateway │◀────────│                   │──▶ 88xx, Gigaset …
│              │◀── APNs VoIP-Push ──│ cme-push-relay│         └──────────────────┘
└──────┬───────┘    (unterwegs)      └──────▲───────┘
       │ HTTPS /client/v1 (App-Schlüssel)   │ Volume softphone-data (APNs-Schlüssel, Secret, Status)
       └──────────────────────────▶ cme-web ┘ ── Aufnahme ein/aus ──▶ cme-recorder (SIPREC)
```

* Die App meldet sich über Flexisip bei CME an. CME kennt sie wie einen Jabber-Client am
  **Gerätenamen** `CSF<Nebenstelle>` (Contact-Parameter `+u.sip!devicename.ccm.cisco.com`).
* Schläft die App, ruft Flexisip den push-relay: im Heim-WLAN direkt über Local Push, sonst APNs.
* Alles Weitere (Gateway, CME-Adresse, Local-Push-Secret, WLAN-Namen, Voicemail-Nummer) holt die App
  aus cme-web. In der App stehen nur **Nebenstelle**, **cme-web-Adresse** und **App-Schlüssel**.
* RTP läuft direkt zwischen Gerät und CME/Telefonen – unterwegs also über VPN ins Heimnetz.

## Einrichtung

### 1. Stack (cme-web)

Flexisip und push-relay sind Teil des cme-web-Stacks (`docker-compose.yml` bzw.
`docker-compose.traefik.yml` im Hauptordner). Zusätzliche Variablen in Portainer:

| Variable | Standard | Bedeutung |
|---|---|---|
| `VM_HOST_IP` | – (Pflicht) | LAN-IP des Docker-Hosts, Flexisip lauscht darauf |
| `SIP_GW_NAME` | leer | DNS-Name für die Apps, z. B. `sip-gw.wiederhol.de` (lokaler DNS-Eintrag → `VM_HOST_IP`) |
| `SIP_GW_PORT` | `5062` | SIP-Port von Flexisip (5060 = Voicemail, 5070 = Recorder) |
| `RELAY_LOCALPUSH_PORT` | `5072` | Local Push (TCP) |
| `RELAY_HTTP_PORT` | `8092` | Flexisip → Relay, nur 127.0.0.1 |
| `APNS_TEAM_ID` | leer | Vorbelegung im Tab Softphone |

Flexisip baut beim ersten Mal 20–40 Minuten. Vorher per SSH bauen spart den Timeout in Portainer:
`docker build -t flexisip:local ./flexisip`. Die Konfiguration erzeugt der Container beim Start aus
`flexisip/flexisip.conf.template` – kein Kopieren und kein `sed` mehr. Eigene Änderungen:
`flexisip.local.conf` ins Volume `flexisip-conf` legen (ersetzt die Vorlage).

**Umzug vom alten Stack `cme-softphone`:** den alten Stack in Portainer löschen (Container `flexisip`
und `cme-push-relay` belegen sonst dieselben Ports). Den APNs-Schlüssel einmal neu in cme-web hochladen.

**Firewall (UDM):**
* WLAN/VPN → Docker-Host: TCP/UDP 5062, TCP 5072
* CME → Docker-Host: TCP/UDP 5062 (eingehende Anrufe an die Apps)
* Docker-Host → CME: TCP/UDP 5060
* Apps → cme-web (HTTPS über Traefik; die IP-Allowlist muss WLAN und VPN enthalten)

### 2. cme-web → Tab Softphone

1. **Push-Gateway**: Name/IP und Ports prüfen, **WLAN-Namen** (SSIDs) für Local Push eintragen.
2. **APNs**: Apple Developer → Keys → + → „Apple Push Notifications service (APNs)“, Environment
   *Sandbox & Production*, *Team Scoped* → `.p8` hochladen. Key-ID kommt aus dem Dateinamen.
3. **App-Schlüssel** kopieren (für die Apps).
4. Tab **Telefone**: je Gerät ein Telefon vom Typ **CME Phone** anlegen (Gerätename `CSF21` usw.).

### 3. Apple Developer Portal (einmalig)

* App-IDs `de.wiederhol.cmephone` (Push Notifications, App Groups, Network Extensions) und
  `de.wiederhol.cmephone.localpush` (App Groups, Network Extensions), App Group `group.de.wiederhol.cmephone`.
* `de.wiederhol.cmephone.mac` für den Mac (App Groups).
* Local Push (`app-push-provider`) und „Default Calling App“ (`com.apple.developer.calling-app`)
  stehen in den Entitlements. Meckert Xcode beim Signieren, die Zeile vorerst aus `project.yml` nehmen.

### 4. Bauen

```bash
cd ~/Projekte/cme-web/softphone
zsh build.command            # erzeugt die Xcode-Projekte und baut iOS + macOS (ohne Signierung)
SIGN=1 zsh build.command     # signiert (Xcode → Einstellungen → Accounts angemeldet)
```

Oder in Xcode: `xcodegen generate` bzw. `xcodegen generate --spec project-mac.yml`, dann öffnen.

### 5. iPhone / iPad

Einstellungen-App → **Apps → CME Phone**:

* **Nebenstelle** (z. B. 21), **cme-web-Adresse** (`https://cme.wiederhol.de`), **App-Schlüssel**
* Codecs, Video, Local Push nach Wunsch. „Anmeldung“ oben zeigt den Status.

Einmal die App öffnen (Mikrofon, lokales Netz und Mitteilungen erlauben). Danach braucht man sie nicht
mehr: Anrufe kommen über CallKit, gewählt wird in der Telefon-App über **Zuletzt** oder **Kontakte**
(„Anrufen mit … CME Phone“) oder per Siri. Ab iOS 18.2 lässt sich CME Phone unter
**Einstellungen → Apps → Standard-Apps → Anrufe** als Standard wählen; dann öffnen `tel:`-Links die App.
Die Ziffern-Tastatur der Telefon-App wählt immer über das Mobilnetz – das lässt iOS nicht zu ändern.

### 6. Mac

Menüleiste (Hörer-Symbol):

* **Softphone / Telefonsteuerung** – Umschalten (Häkchen = aktiv)
* **Anrufliste** – Wahlwiederholung (⌘R) und Rückruf (die Nummer wird unverändert gewählt)
* **Wähltastatur** (⌘D) – Tastenfeld; Leer + Anrufen holt die letzte Nummer
* **Einstellungen** (⌘,) – cme-web-Adresse, App-Schlüssel, Nebenstelle des Macs, Tischtelefon

**Call-Toast** unten rechts mit Bild und Name aus den Kontakten:
* eingehend: **Voicemail** (486 → CME leitet per „call-forward busy“ an die Voicemail),
  **Ablehnen** (603), **Annehmen**
* im Gespräch: **Aufnahme** und **Auflegen**. Externe Gespräche schaltet der Knopf beim cme-recorder ein
  und aus (beide Modi), interne im Softphone-Modus als lokale WAV-Datei in *Musik/CME Phone*.
* bei Telefonsteuerung meldet der Toast die Anrufe am Tischtelefon (Statusabo); Annehmen, Ablehnen,
  Voicemail und Auflegen schicken die eingestellten Tasten-URIs ans Telefon.

**Telefonsteuerung** einrichten: `cme/deskphone-control.cfg` (url authentication, Presence),
in den Einstellungen IP, Nebenstelle und Zugangsdaten des Telefons. Welche `Key:`-URIs wirken
(Annehmen, Ablehnen, iDivert …), hängt von Modell und Firmware ab – unter *Tischtelefon → Ausprobieren*
testen. CSTA gibt es auf CME ab 12.6 nicht mehr und galt nur für SCCP-Telefone; deshalb Cisco XML.

### 7. Anrufe aus dem Internet (optional)

Kamailio nimmt dann Anrufe an `<name>@<SIP_PUBLIC_DOMAIN>` an und gibt sie an die Rufnummer bei der CME weiter.
`<name>` ist der Teil vor dem `@` der E-Mail-Adressen im Tab **Benutzer**, sofern deren Domain `SIP_PUBLIC_DOMAIN` ist.
Angerufen wird die erste Rufnummer des Benutzers mit CME Phone (Mobilgerät), sonst die erste, die kein Fax ist.

1. **`.env`:**
   - `SIP_PUBLIC_DOMAIN` setzen.
   - `SIP_GW_NAME` muss draußen auf die öffentliche IP zeigen. Intern darf der Name auf den Docker-Host zeigen.
2. **DNS (öffentlich):**
   - `_sip._tcp.<domain>  SRV 10 0 5060 <SIP_GW_NAME>`
   - `_sips._tcp.<domain> SRV 10 0 5061 <SIP_GW_NAME>`
   - Für TLS heißt der Name `_sips._tcp`, nicht `_sips._tls`.
3. **Router:** Portweiterleitungen an `VM_HOST_IP`:
   - TCP 5060 und TCP 5061, UDP 5060 auf `EXT_UDP_PORT` (5067; 5060 belegt dort Asterisk)
   - UDP `APP_RTP_PORTS` (40000–40199)
   - Für UDP zusätzlich `_sip._udp.<domain> SRV 10 0 5060 <SIP_GW_NAME>`.
4. **Zertifikat:** Traefik holt es über den Router `cme-web-sipgw` (`docker-compose.traefik.yml`). Kamailio liest es aus der `acme.json` (`TRAEFIK_ACME_JSON`) und lädt Erneuerungen selbst neu. Ohne Traefik: `TLS_CERT_DIR` mit `cert.pem` und `key.pem`.
5. **CME:** einen eingehenden Dial-Peer für Kamailio anlegen (Port `CME_EXT_PORT`, 5066) und den Docker-Host als vertrauenswürdig eintragen:
   ```
   voice service voip
    ip address trusted list
     ipv4 192.168.1.110
   voice class uri KAMAILIOEXT sip
    pattern 192\.168\.1\.110:5066
   dial-peer voice 5066 voip
    description Anrufe aus dem Internet (Kamailio)
    session protocol sipv2
    session transport udp
    incoming uri via KAMAILIOEXT
    voice-class codec 3
    dtmf-relay rtp-nte
    no vad
   ```

**Schutz:**
- Kamailio lässt von draußen ohne Anmeldung nur INVITEs an bekannte Adressen durch, alles andere bekommt 404.
- Wer zu viele Anfragen schickt, ist 10 min gesperrt (pike).
- Anmelden dürfen sich die Apps von draußen nur über TLS.
- Die Medien laufen über die Schnittstelle `ext` von rtpengine, mit der öffentlichen IP im SDP.

## Was CME bei den Codecs mitmacht

| Codec | App ↔ Cisco 88xx / Gigaset / Amt | App ↔ App |
|---|---|---|
| G.711 | ✅ | ✅ |
| G.722 | ✅ | ✅ |
| Opus | ❌ CME kann nicht umkodieren | ⚠️ nur mit `codec transparent` (Experiment in `cme/softphone.cfg`) |
| H.264 | ✅ mit 8845/8865 | ✅ (nur iOS) |
| H.265 | ❌ | ⚠️ wie Opus |

## Testplan

| # | Test | Erwartung | Wenn nicht |
|---|---|---|---|
| 1 | Tab Softphone | push-relay „läuft“, APNs „eingerichtet“ | Container-Log cme-push-relay |
| 2 | App: Einstellungen eingetragen | „Angemeldet“; cme-web zeigt den Pool CSF21 registriert | Flexisip-Log; Gerätename/Typ im Pool |
| 3 | 21 → 10 | Verbindung, G.722 | `debug ccsip messages` |
| 4 | 10 → 21, App beendet, im WLAN | klingelt über Local Push | Tab Softphone: Gerät unter „Im WLAN verbunden“? |
| 5 | wie 4, mobil + VPN | klingelt über APNs | Tab Softphone: „Letzter APNs-Versand“ |
| 6 | Mac: Anruf auf 23 | Toast, Voicemail-Knopf landet im Postfach | Postfach für 23 in cme-web an? |
| 7 | Mac: externes Gespräch, Aufnahme | Knopf rot, Aufnahme erscheint in cme-web → Aufzeichnung | Aufzeichnung für den Trunk an? |
| 8 | Mac: Telefonsteuerung, Anruf auf 10 | Toast „Tischtelefon“, Annehmen nimmt am Telefon ab | Event-Paket dialog ↔ presence |

## Ordner

```
project.yml / project-mac.yml   XcodeGen (iOS mit Local-Push-Extension / macOS)
build.command                   erzeugen + bauen (Logs in build-logs/)
CMEPhone/Core/                  gemeinsam: SIPManager (liblinphone), PhoneSettings, ClientAPI (cme-web),
                                RecordingController; nur iOS: CallKitProvider, LocalPushController
CMEPhone/Settings.bundle        Einstellungen-App (iOS)
CMEPhone/UI/ContentView.swift   iOS: nur Status + Aufnahme-Knopf
CMEPhoneMac/App/                Menüleisten-App, CallCenter, Anrufliste, Kontakte
CMEPhoneMac/UI/                 Menü, Call-Toast, Wähltastatur, Einstellungen
CMEPhoneMac/DeskPhone/          Cisco XML Execute, Statusabo (dialog-info / PIDF)
LocalPushProvider/              NEAppPushProvider (TCP zum push-relay)
cme/                            Beispiel-Konfiguration für CME
tests/run.sh                    Tests ohne Xcode (Swift + Python)
```

Lizenz: Linphone SDK ist **AGPLv3** – privat kein Problem, für den App Store bräuchte es eine
kommerzielle Lizenz (oder einen anderen SIP-Kern, z. B. baresip).
