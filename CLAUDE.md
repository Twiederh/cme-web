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
| Flexisip | `flexisip/` (Template `flexisip.conf.template` → `/run/flexisip.conf`) | SIP-Proxy für die Apps, Push-Gateway |
| push-relay | `push-relay/relay.py` | APNs-Versand; läuft mit Host-Netz auf 127.0.0.1:8092 |
| asterisk, recorder, whisper | jeweilige Ordner | Voicemail/Aufnahme/Transkription |
| Softphone | `softphone/` (`CMEPhone` = iOS, `CMEPhoneMac` = Menüleisten-App, `Shared`) | liblinphone-basiert, CallKit auf iOS |

**Deployment:**
- GitHub Actions bauen die Images nach `ghcr.io/twiederh/...`.
- Auf docker01 (192.168.1.110) wird per Portainer gepullt und neu gestartet.
- Watchtower ist für alle Stack-Dienste per Label `com.centurylinklabs.watchtower.enable=false` ausgeschlossen (inzwischen ganz gelöscht).
- Portainer-GitOps für Stack `cmeconfig`: „Force redeployment“ muss aus sein. Sonst baut Portainer den Stack alle 5 min neu auf, Flexisip verliert jedes Mal alle Anmeldungen (iPhone: „io-Error“).
- Die CME ist für `show`-Befehle per `ssh thomas@192.168.1.4` erreichbar.

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

- [ ] iPhone hält → 8845/Mac sehen nichts. Ursache: CME ordnet INVITEs über die IP im Contact einem Pool zu, über Flexisip ist das die iPhone-IP statt der angemeldeten (Flexisip) → kein Pool, keine Shared Line, `Replaces` reicht die CME dann an den Trunk weiter (Rückruf aufs Handy). `masquerade-contacts-for-invites` getestet: Zuordnung klappt (pid 40002), aber Flexisip verwirft danach ACK/re-INVITE/BYE (482 Loop Detected). Plan: Flexisip als B2BUA (Build mit B2BUA, Patch für `Call-Info` im re-INVITE).
- [ ] iPhone klingelt nach Annahme am Mac noch ~10 s weiter (SIP-CANCEL kommt sofort an, Ursache in der App; Protokoll des iPhones auswerten).
- [ ] Optional: Flexisip-Registrierungen in Redis speichern.
- [ ] Aufräumen: Testkonfiguration auf der CME, veraltete Swift-APIs, Passwörter erneuern, die im Chat standen.
- [ ] Entscheidung App Store / Android / Windows. liblinphone ist GPLv3 bzw. proprietär lizenziert, was für den App Store ein Problem ist. Optionen: Lizenzpreis bei Belledonne anfragen oder auf baresip (BSD) wechseln.
