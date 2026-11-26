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
| Flexisip | `flexisip/` (Template `flexisip.conf.template` → `/run/flexisip.conf`) | SIP-Proxy für die Apps, Push-Gateway, B2BUA zur CME |
| push-relay | `push-relay/relay.py` | APNs-Versand; läuft mit Host-Netz auf 127.0.0.1:8092 |
| asterisk, recorder, whisper | jeweilige Ordner | Voicemail/Aufnahme/Transkription |
| Softphone | `softphone/` (`CMEPhone` = iOS, `CMEPhoneMac` = Menüleisten-App, `Shared`) | liblinphone-basiert, CallKit auf iOS |

**Deployment:**
- GitHub Actions bauen die Images nach `ghcr.io/twiederh/...`.
- Auf docker01 (192.168.1.110) wird per Portainer gepullt und neu gestartet.
- Watchtower ist für alle Stack-Dienste per Label `com.centurylinklabs.watchtower.enable=false` ausgeschlossen (inzwischen ganz gelöscht).
- Portainer-GitOps für Stack `cmeconfig`: „Force redeployment“ muss aus sein. Sonst baut Portainer den Stack alle 5 min neu auf, Flexisip verliert jedes Mal alle Anmeldungen (iPhone: „io-Error“).
- Die CME ist für `show`-Befehle per `ssh thomas@192.168.1.4` erreichbar.
- Voicemail-Ansagen: deutscher 16-kHz-Satz von asterisksounds.org (2.11.19, CC BY-SA 3.0). `asterisk/Dockerfile` lädt ihn aus dem Internet Archive und prüft per SHA-256. Die alten GSM-Ansagen aus `asterisk-prompt-de` waren übersteuert und klangen mit G.722 nach periodischem Knacksen. Die Domain asterisksounds.org leitet inzwischen auf eine Casino-Seite um, also nicht von dort laden.
- CPU-Vorrang in den Compose-Dateien: Asterisk und Recorder `cpu_shares: 4096`, Flexisip 2048, whisper 128 mit `cpus: 2`. Grund: whisper belegt beim Transkribieren alle 4 Kerne, die Ansagen stocken dann.

**Kommunikation cme-web → push-relay:**
- Beide nutzen das gemeinsame Volume `softphone-data` (in cme-web unter `/data/push`, im Relay unter `/data`).
- cme-web legt Jobs als `outbox/*.json` ab, `outbox_loop` im Relay verschickt sie.
- Jobs, die älter als 60 s sind, werden verworfen.

## Netz und Geräte

- **CME (vcube):** 192.168.11.4
- **Cisco 8845:** 192.168.11.46, Pool 4
- **Gigaset N610 IP Pro:** 192.168.11.6, Pool 1
- **Mac mini:** 192.168.7.116, Pool 6 (SIPMacMini)
- **iPhone (MOBIphone):** über Flexisip, MOB-Pool `MOB4961874191283`
- **Testnummer:** +4961874191283 („-283“), alle Geräte als Shared Line
- **Externer Zugang:** WireGuard-VPN. Fürs Klingeln unterwegs sorgt der APNs-Push über Flexisip.

## Eigenheiten der CME (hart erarbeitet)

**Codecs**
- Die CME hat keinen Transcoder. Wenn das übernehmende Gerät einen anderen Codec spricht als der Trunk (sipgate), meldet die CME `%VOICE_IEC-3-GW: CCAPI: Internal Error` und legt auf.
- Beim Übernehmen per `Replaces` antwortet die CME dem Gerät in der Reihenfolge **seines** Angebots, nicht nach ihrer Codec-Liste. Das übernehmende Gerät muss also den Codec des Trunks zuerst anbieten.
- Die CME schreibt in die cnf `<preferredCodec>g722-64</preferredCodec>` (ihr Codec-Name). Die 8845-Firmware (CUCM-Stand) versteht nur `g722` und setzt sonst PCMU an die erste Stelle. Deshalb überschreibt eine phonecfg-Regel in cme-web den Wert in `SEP5006AB818C20.cnf.xml` mit `g722`. Seitdem klappt die Übernahme Mac → 8845 (getestet 25.11.).
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

## Flexisip und Push

- Registrierungen liegen nur im Speicher. Nach einem Neustart von Flexisip muss die App einmal geöffnet werden, sonst gibt es 404 und keinen Push. Redis als Speicher wäre eine Option (noch offen).
- `max-expires=604800` (7 Tage). Die App registriert auf iOS mit 7 Tagen, auf macOS mit 600 s. `sharedline.APP_MAX_EXPIRES` ist entsprechend gesetzt.
- **Digest für App-Anfragen:** REGISTER prüft cme-web (`sharedline._app_register`), alle anderen Anfragen der Apps prüft Flexisip (`module::Authentication`, trusted: CME, 127.0.0.1).
  - Die Passwörter schreibt cme-web nach `/data/push/flexisip-passwd` (`phoneapp.write_flexisip_passwd`), mit einer Zeile je Gerät und From-Domain (SIP-Domain, CME-IP, Flexisip-IP, Apps-Domain des B2BUA).
  - In der Datei steht der fertige MD5-HA1 aus Anmeldename, Realm und Passwort, nicht `clrtxt`. Aus `clrtxt` bildet Flexisip den Hash mit der Rufnummer statt mit dem Anmeldenamen, die App bekommt dann `403 Forbidden`.
  - `entrypoint.sh` prüft die Zeilen und kopiert die Datei nach `/run/flexisip-passwd`.
  - Flexisip bricht ab, wenn die Datei fehlt oder eine Zeile nicht der Grammatik entspricht. Schon `(` in einem Kommentar reicht.

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

**Swift-Stolperstein:** linphonesw hat eine eigene Klasse `Range`, die das Swift-`Range` überdeckt. Deshalb im Code `Swift.Range` schreiben.

## Offene Punkte

- [ ] **B2BUA testen** (noch ungetestet). Hintergrund: Die CME ordnet INVITEs über IP:Port im Contact einem Pool zu. Über den reinen Proxy stand dort die iPhone-Adresse. Die CME führte iPhone-Gespräche deshalb nicht auf der Shared Line: Halten war unsichtbar, und ein `Replaces` vom Mac reichte sie an den Trunk weiter oder leitete per 302 auf die -283 um.
  - Jetzt läuft jedes Gespräch der Apps über den B2BUA in Flexisip (`[b2bua-server]`, UDP 5064, RTP 40000–40199). cme-web meldet den MOB-Pool mit dieser Adresse an (`B2BUA_PORT`).
  - `flexisip/patch-b2bua-cisco.py` sorgt dafür, dass re-INVITEs frische Parameter bekommen (kein `Replaces` aus dem ersten INVITE) und `Call-Info: remotecc` übernehmen.
  - `no-rtp-timeout` steht auf 6 h, sonst würde der B2BUA gehaltene Gespräche nach 30 s beenden.
  - Der Umweg `/client/v1/held` (iPhone meldet Halten an cme-web) ist entfernt.
- [ ] Testen nach Deploy des push-relay: Annehmen am iPhone und kein Weiterklingeln nach Annahme am Mac. Ursache war, dass der Relay die Call-ID im VoIP-Push auf die oberste Ebene legte. liblinphone sucht sie wie bei Flexisip in `aps` (`relay.voip_payload`). Ohne sie legt liblinphone einen zweiten Anruf an. Annehmen beendete dann den echten (603 Decline), der Push-Anruf klingelte ~10 s nach.
- [ ] Optional: Flexisip-Registrierungen in Redis speichern.
- [ ] Aufräumen: Testkonfiguration auf der CME, veraltete Swift-APIs, Passwörter erneuern, die im Chat standen.
- [ ] Entscheidung App Store / Android / Windows. liblinphone ist GPLv3 bzw. proprietär lizenziert, was für den App Store ein Problem ist. Optionen: Lizenzpreis bei Belledonne anfragen oder auf baresip (BSD) wechseln.
