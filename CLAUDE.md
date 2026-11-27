# CLAUDE.md – cme-web / CME Phone

Weboberfläche und Dienste rund um eine Cisco CME (Call Manager Express) plus das eigene Softphone „CME Phone“ (iOS + macOS).
Sprache im Projekt: Deutsch (UI-Texte, Logs, Commit-Messages).

## Arbeitsregeln

- **Commits:** Der Nutzer committet und pusht normalerweise selbst. Commit-Befehle vorschlagen; nur selbst committen, wenn er es ausdrücklich sagt.
- **Keine E-Mail-Adressen und keine Passwörter** in Befehle, Code, Commits oder diese Datei schreiben.
- **`.github/workflows` nicht anfassen**, ohne vorher zu fragen. Die Workflows bauen die Docker-Images.
- Vor jedem Kommando sicherstellen, dass das Arbeitsverzeichnis `~/Projekte/cme-web` ist. Der alte Ordner „CME phone“ ist kein Git-Repo.

## Aufbau

| Teil | Ort | Zweck |
|---|---|---|
| cme-web (Flask) | `cme_web.py`, `phoneapp.py`, `sharedline.py`, … | Weboberfläche, Provisionierung der Apps (`/client/v1/...`), Shared-Line-Agent |
| Kamailio | `kamailio/` (Template `kamailio.cfg.template` → `/run/kamailio/kamailio.cfg`) | Registrar und Proxy für die Apps (TCP 5062), Push über push-relay, Weg zur CME über UDP 5064 (Contact des MOB-Pools) |
| rtpengine | `rtpengine/` | Medien-Anker: SRTP (SDES) zu den iOS-Geräten, RTP zur CME, Ports 40000–40199 |
| push-relay | `push-relay/relay.py` | APNs-Versand; läuft mit Host-Netz auf 127.0.0.1:8092 |
| asterisk, recorder, whisper | jeweilige Ordner | Voicemail/Aufnahme/Transkription |
| Softphone | `softphone/` (`CMEPhone` = iOS, `CMEPhoneMac` = Menüleisten-App, `Shared`) | baresip über `third_party/` (CMESIP.xcframework, C-Schicht `cmesip`), gemeinsamer `CMEPhone/Core/SIPManager.swift`; iOS mit CallKit und eigenem PushKit |

**Deployment:**
- GitHub Actions bauen die Images nach `ghcr.io/twiederh/...`.
- Auf docker01 (192.168.1.110) wird per Portainer gepullt und neu gestartet.
- Portainer deployt den Stack nach jedem Push auf `main` automatisch neu. Laufende Gespräche brechen dabei ab, also nicht während eines Tests pushen.
- Watchtower ist für alle Stack-Dienste per Label `com.centurylinklabs.watchtower.enable=false` ausgeschlossen (inzwischen ganz gelöscht).
- Portainer-GitOps für Stack `cmeconfig`: „Force redeployment“ muss aus sein. Sonst baut Portainer den Stack alle 5 min neu auf.
- Keine Testcontainer von Hand unter den Namen des Stacks (`cme-kamailio` …) starten: Dann scheitert „Pull and redeploy“, und die übrigen Container bleiben auf „Created“.
- Die CME ist für `show`-Befehle per `ssh thomas@192.168.1.4` erreichbar.
- Voicemail-Ansagen: deutscher 16-kHz-Satz von asterisksounds.org (2.11.19, CC BY-SA 3.0). `asterisk/Dockerfile` lädt ihn aus dem Internet Archive und prüft per SHA-256. Die alten GSM-Ansagen aus `asterisk-prompt-de` waren übersteuert und klangen mit G.722 nach periodischem Knacksen. Die Domain asterisksounds.org leitet inzwischen auf eine Casino-Seite um, also nicht von dort laden.
- CPU-Vorrang in den Compose-Dateien: Asterisk und Recorder `cpu_shares: 4096`, Kamailio 2048, rtpengine 4096, whisper 128 mit `cpus: 2`. Grund: whisper belegt beim Transkribieren alle 4 Kerne, die Ansagen stocken dann.

**Kommunikation cme-web → push-relay:**
- Beide nutzen das gemeinsame Volume `softphone-data` (in cme-web unter `/data/push`, im Relay unter `/data`).
- cme-web legt Jobs als `outbox/*.json` ab, `outbox_loop` im Relay verschickt sie.
- Jobs, die älter als 60 s sind, werden verworfen.

## Netz und Geräte

- **CME (vcube):** 192.168.11.4
- **Cisco 8845:** 192.168.11.46, Pool 4
- **Gigaset N610 IP Pro:** 192.168.11.6, Pool 1
- **Mac mini:** 192.168.7.116, Pool 6 (SIPMacMini)
- **iPhone (MOBIphone):** über Kamailio, MOB-Pool `MOB4961874191283`
- **Testnummer:** +4961874191283 („-283“), alle Geräte als Shared Line
- **Externer Zugang:** WireGuard-VPN. Fürs Klingeln unterwegs sorgt der APNs-Push über Kamailio.

## Eigenheiten der CME (hart erarbeitet)

**Codecs**
- Die CME hat keinen Transcoder. Wenn das übernehmende Gerät einen anderen Codec spricht als der Trunk (sipgate), meldet die CME `%VOICE_IEC-3-GW: CCAPI: Internal Error` und legt auf.
- Beim Übernehmen per `Replaces` antwortet die CME dem Gerät in der Reihenfolge **seines** Angebots, nicht nach ihrer Codec-Liste. Das übernehmende Gerät muss also den Codec des Trunks zuerst anbieten.
- Die CME schreibt in die cnf `<preferredCodec>g722-64</preferredCodec>` (ihr Codec-Name). Die 8845-Firmware (CUCM-Stand) versteht nur `g722` und setzt sonst PCMU an die erste Stelle. Deshalb überschreibt eine phonecfg-Regel in cme-web den Wert in `SEP5006AB818C20.cnf.xml` mit `g722`. Seitdem klappt die Übernahme Mac → 8845 (getestet 25.11.). Das 8845 liest die Datei nur beim Start: Bietet es beim Übernehmen PCMU zuerst an (Abbruch mit `Internal Error`), das Telefon neu starten (`voice register pool 4` → `restart`).
- Getestet und verworfen: `voice-class sip midcall-signaling passthru media-change` auf dial-peer 1/2. Die CME schickt das re-INVITE zum Trunk, wartet die Antwort aber nicht ab. `codec g722-64` direkt im Pool 4 funktioniert, ist aber für Anrufe nach draußen zu riskant (nur ein Codec).
- Ein Pool ohne Codec-Angabe bekommt G.729 angeboten. liblinphone kann kein G.729, die Folge ist 488. Deshalb setzt cme-web für MOB-Pools `voice-class codec 3` (G.722 + G.711), siehe `MOBILE_CODEC` in `phoneapp.py`.

**Halten auf der Shared Line**
- Die anderen Geräte sehen ein Halten nur, wenn der re-INVITE `Call-Info: <urn:x-cisco-remotecc:hold>` bzw. `<…:resume>` trägt (sendonly/sendrecv). `inactive` ist nicht nötig.
- Der From muss die CME-IP enthalten (`sip:<nr>@192.168.11.4`), nicht `cme.local`. Sonst ordnet die CME das Gespräch nicht der Shared Line zu.

**Übernehmen**
- Ein `INVITE` an die Remote-URI mit `Replaces: <call-id>;to-tag=<local-tag des Halters>;from-tag=<remote-tag (CME)>`.
- Ein re-INVITE, der noch einen `Replaces`-Header trägt, wird von der CME ignoriert („invalid IE content“). Deshalb baut `SIPManager.updateParams(for:)` die Parameter immer frisch, denn `createCallParams(call:)` kopiert Custom-Header mit.

**dialog-NOTIFY**
- Gehalten erscheint als `<param pname="+sip.rendering" pval="no"/>`. Cisco schreibt `pval`, nicht `pvalue`.
- Beim Fortsetzen fehlen `<local>`/`<remote>`.
- Partial-Notifies wiederholen sich. Die Regex muss `<dialog\s` lauten, sonst passt sie auch auf `<dialog-info`.

**403 Forbidden**
- Kommt bei doppelter Registrierung bzw. wenn ein Unregister nicht ankam.
- Die App versucht es alle 10 s erneut. Jabber-Modus: expires 60.

## Kamailio, rtpengine und Push

- **Anmeldung:** Kamailio prüft den Digest selbst (Realm = SIP-Domain). Die Passwörter kommen aus `/data/push/flexisip-passwd` von cme-web (MD5-HA1 aus Anmeldename, Realm und Passwort). `passwd2htable.py` macht daraus db_text-Tabellen, `entrypoint.sh` lädt sie alle 20 s neu. Die Nummer im To muss zum Anmeldenamen passen, sonst 403.
- Nach jeder Anmeldung schickt Kamailio cme-web einen NOTIFY (`Event: x-cme-app`, Port 5074), damit der Tab „Softphone“ das Gerät als angemeldet zeigt (`sharedline._app_registered`).
- Anmeldungen bleiben über Neustarts erhalten (usrloc in db_text im Volume `kamailio-data`). `max_expires` 604800, iOS meldet sich mit 7 Tagen an.
- **GRUU ist abgeschaltet** (`gruu_enabled 0`). Sonst nimmt liblinphone `sip:<nr>@cme.local;gr=…` als Contact, und UPDATE/BYE der CME an die App scheitern mit 478.
- **topos** versteckt die App-Adressen vor der CME (Speicher: db_text). Die Tabellen `topos_d`/`topos_t` müssen leere Spalten erlauben, das stellt `entrypoint.sh` beim Start sicher. Sonst „null value not allowed“, und eingehende Anrufe kommen nicht zur App.
- **Eingehend:** Kamailio schickt über push-relay (`GET /push` auf 127.0.0.1:8092) einen VoIP-Push an jedes Gerät mit `pn-prid` und wartet bis 30 s auf die Anmeldung (tsilo).
- **SRTP:** Geräte mit `pn-provider` im Contact (iOS) bekommen RTP/SAVP, alle anderen RTP/AVP. Pro Call-ID merkt sich Kamailio die Wahl (htable `srtp`), damit re-INVITEs der CME passen. `APP_SRTP=0` schaltet SRTP ab. Die iOS-App bietet über das Gateway immer SRTP an (nicht verpflichtend). Die Schlüssel (SDES) laufen noch über TCP ohne TLS.
- Der Mac meldet sich direkt an der CME an (Pool 6) und läuft nicht über Kamailio.
- **Von außen** (seit 27.11., nur wenn `SIP_PUBLIC_DOMAIN` gesetzt ist und ein Zertifikat da ist; im Template `#!ifdef WITH_EXT`):
  - Kamailio hört auf TCP 5060 und TLS 5061 unter `SIP_GW_NAME`.
  - Anrufe an `<name>@<SIP_PUBLIC_DOMAIN>` werden über `sip-aliases` (`phoneapp.write_sip_aliases`, Quelle: E-Mail-Adressen im Tab Benutzer) auf eine Rufnummer abgebildet. Sie gehen über UDP 5066 an die CME, dort ist ein eigener Dial-Peer nötig (README Abschnitt 7).
  - rtpengine hat dafür die Schnittstelle `ext` (öffentliche IP im SDP, `direction=ext`).
  - Das Zertifikat kommt aus Traefiks `acme.json` (DNS-Challenge bei All-Inkl).
  - Intern zeigt `sip-gw.wiederhol.de` auf docker01, und `_sip._tcp.wiederhol.de` im internen DNS zeigt auf CUCM/vcube.
- **Test-Stolperstein:** Ruft ein Gerät derselben Nummer (iPad → -283) an, gehört der Anruf zum MOB-Pool. Die CME schickt dem MOB-Pool dann keine dialog-NOTIFYs, es gibt also keinen Push beim Halten. Zum Testen von einer anderen Nummer anrufen.

**APNs**
- Alert-Push mit Topic `de.wiederhol.cmephone`, Kategorie `HELD_CALL`, Aktion `TAKEOVER` („Übernehmen“).
- Für „Halten beendet“ geht ein Hintergrund-Push mit `content-available`.
- Bei `BadDeviceToken` wird automatisch die andere Umgebung (sandbox/prod) versucht.

**Ablauf „gehalten → Mitteilung“**
1. `sharedline._track_held` wertet die dialog-NOTIFYs der MOB-Subscriptions aus.
2. Von dort geht es über `notify_held` (phoneapp) und `queue_push` in die Outbox und weiter ans Relay.
3. Auf dem iPhone zeigt `HeldCallNotifications` die Mitteilung. Tippt man auf „Übernehmen“, startet `CallKitProvider.startTakeover` und dann `SIPManager.takeOver`.
4. Auf dem Mac erscheint stattdessen ein Toast in `CallToast.swift` mit „Tink“ als Doppelton.

## Bauen und Testen

**Python-Tests:**
```
python3 tests/test_sharedline.py
python3 tests/test_phoneapp.py
python3 tests/test_relay.py
```

**Apps:** `softphone/build.command`
- `ONLY=ios` oder `ONLY=mac` baut nur eine Plattform.
- iOS wird dabei nur kompiliert (ohne `SIGN=1` unsigniert). Aufs Gerät per Xcode (⌘R) oder Product → Archive → TestFlight.
- Build-Nummer = Anzahl der Commits. Uncommittete Builds tragen dieselbe Nummer mit `+` am Hash, also vor dem Testen committen. Die App zeigt sie an (iOS: Startbildschirm unten).
- Der Mac-Build wird immer signiert (`MACSIGN=1`). Unsigniert fehlt die Sandbox, dann sind die Einstellungen in `~/Library/Containers/de.wiederhol.cmephone.mac` nicht sichtbar.
- Das Skript beendet die laufende Mac-App, installiert sie nach `/Applications/CMEPhone.app` und startet sie neu. Ein Bundle im laufenden Betrieb zu ersetzen bringt die App zum Absturz.

**baresip (`softphone/third_party/`):** `fetch.sh` lädt re, baresip, libg722 und opus mit festen Commits und spielt `patches/` ein. `build.sh` baut daraus `build/CMESIP.xcframework`. `build.command` startet beides selbst, wenn das Framework fehlt oder `cmesip/` bzw. `patches/` neuer sind. Test der C-Schicht: `zsh third_party/tests/run_call_test.sh`. Mit TCP steht im Contact der Port der Verbindung, nicht 5060. CME stellt Anrufe trotzdem zu, über die bestehende Verbindung (getestet 27.11.).

**iOS-Eigenheiten mit baresip:**
- CallKit gibt die Audio-Sitzung erst nach dem Annehmen frei. baresip startet den Ton vorher, iOS lehnt das ab (`'!pri'`). Bei didActivate baut `cmesip_audio_session(true)` den Ton per `audio_update` neu auf.
- Jede Anmeldung lässt Kamailio (tsilo) das INVITE erneut zustellen. Ein zweites INVITE mit bekannter Call-ID lehnt cmesip mit 482 ab, ein zweiter Push zum selben Anruf meldet nicht erneut an.
- Feste `+sip.instance` (UserDefaults `sipInstanceUUID`), damit Kamailio die Anmeldung des Geräts ersetzt.
- Protokoll live vom angeschlossenen iPhone: `xcrun devicectl device process launch --device <UDID> --terminate-existing --console de.wiederhol.cmephone` (mit `DEVELOPER_DIR` auf Xcode).

**Swift-Stolperstein:** linphonesw hat eine eigene Klasse `Range`, die das Swift-`Range` überdeckt. Deshalb im Code `Swift.Range` schreiben.

## Offene Punkte

- [x] Kamailio + rtpengine statt Flexisip (27.11.): Anmeldung, Push, ein- und ausgehend, SRTP zu iOS, Halten und Übernehmen iPhone ↔ Mac ↔ 8845 getestet.
- [x] Mac-App auf baresip (27.11., Build 311): ein- und ausgehend, Halten, Übernehmen, Voicemail per 302 getestet. Noch ohne Video und lokale Aufnahme.
- [x] iOS-App auf baresip (27.11., Build 316): Push, ein- und ausgehend, SRTP, Halten, Übernehmen, lokale Aufnahme getestet.
- [ ] Video (iOS + Mac). TLS in den Apps (baresip mit OpenSSL bauen; Kamailio kann TLS 5061 schon).
- [ ] Zugang von außen: eingerichtet am 27.11. (Portweiterleitungen, SRV `_sips._tcp`, auf der CME trusted 192.168.1.110, `voice class uri KAMAILIOEXT`, dial-peer 5066). OPTIONS über TCP und TLS an die öffentliche IP gibt 200, das Zertifikat ist von Let's Encrypt. Ein Anruf aus dem Internet ist noch nicht getestet. App-Store-Frage ist mit baresip (BSD) entschärft.
- [x] sipgate-Dial-Peers 1/2 mit `media-class 8999` (27.11.), der cme-recorder sieht jetzt auch sipgate-Gespräche.
- [x] `flexisip/` und das Flexisip-Image entfernt (27.11.). Der Dateiname `flexisip-passwd` bleibt (cme-web schreibt, Kamailio liest).
- [ ] Testen nach Deploy des push-relay: Annehmen am iPhone und kein Weiterklingeln nach Annahme am Mac. Ursache war, dass der Relay die Call-ID im VoIP-Push auf die oberste Ebene legte. liblinphone sucht sie wie bei Flexisip in `aps` (`relay.voip_payload`). Ohne sie legt liblinphone einen zweiten Anruf an. Annehmen beendete dann den echten (603 Decline), der Push-Anruf klingelte ~10 s nach.
- [ ] Aufräumen: Testkonfiguration auf der CME, veraltete Swift-APIs, Passwörter erneuern, die im Chat standen.
- [ ] Entscheidung App Store / Android / Windows. liblinphone ist GPLv3 bzw. proprietär lizenziert, was für den App Store ein Problem ist. Optionen: Lizenzpreis bei Belledonne anfragen oder auf baresip (BSD) wechseln.
