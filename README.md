# CME Web – Weboberfläche für Cisco Call Manager Express (SIP)

Zeigt den Status der SIP-Trunks, Dial-Peers, SIP-Telefone und Rufnummern
und legt Rufnummern (`voice register dn`) und Telefone (`voice register pool`) an,
bearbeitet oder löscht sie – einschließlich Shared Lines.
Der Container verbindet sich per SSH (netmiko) mit dem Router.

## Installation mit Portainer

1. **Stacks → Add stack**, Name z. B. `cme-web`
2. **Build method: Repository**
   - Repository URL: `https://github.com/<benutzer>/cme-web`
   - Repository reference: `refs/heads/main`
   - Compose path: `docker-compose.yml`
   - Bei einem privaten Repo: **Authentication** einschalten, GitHub-Benutzername
     und ein Personal Access Token (Leserechte auf das Repo) eintragen
3. Unter **Environment variables** eintragen:

| Variable | Beispiel | Bedeutung |
|---|---|---|
| `CME_HOST` | `192.168.1.4` | IP des Routers |
| `CME_USER` | `thomas` | SSH-Benutzer (privilege 15) |
| `CME_PASSWORD` | | SSH-Passwort |
| `CME_SECRET` | | enable-Passwort, nur falls nötig |
| `CME_WEB_USER` | `admin` | Login für die Weboberfläche |
| `CME_WEB_PASS` | | Passwort für die Weboberfläche |
| `CME_WEB_PORT` | `8080` | Port auf dem Docker-Host |
| `CME_DEMO` | `false` | `true` = Beispieldaten statt Router |

4. **Deploy the stack**. Portainer baut das Image aus dem Dockerfile.
5. Öffnen: `http://<docker-host>:8080`

Updates: Im Stack **Pull and redeploy** (mit „Re-pull image and redeploy“) klicken,
oder unter **GitOps updates** automatisches Aktualisieren einschalten.

## Hinter Traefik

Für einen vorhandenen Traefik (v3) gibt es `docker-compose.traefik.yml`.
Sie veröffentlicht keinen Port (Zugriff nur über Traefik), sondern hängt den Container ins Traefik-Netz
und setzt die Labels für HTTPS, die Weiterleitung von HTTP auf HTTPS,
eine IP-Allowlist (nur LAN) und Sicherheits-Header.

In Portainer als **Compose path** `docker-compose.traefik.yml` angeben und
zusätzlich zu den Variablen oben setzen:

| Variable | Standard | Bedeutung |
|---|---|---|
| `CME_DOMAIN` | – (Pflicht) | Hostname, z. B. `cme.example.de` |
| `TRAEFIK_NETWORK` | `proxy` | Docker-Netz, in dem Traefik hängt (muss existieren) |
| `TRAEFIK_ENTRYPOINT_HTTPS` | `websecure` | HTTPS-Entrypoint von Traefik |
| `TRAEFIK_ENTRYPOINT_HTTP` | `web` | HTTP-Entrypoint (für die Weiterleitung) |
| `TRAEFIK_CERTRESOLVER` | `letsencrypt` | Name des Certresolvers in Traefik |
| `TRAEFIK_ALLOW` | private Netze | Erlaubte Quell-IPs, kommagetrennt |

Hinweise:
- Traefik v2: in der Datei `ipallowlist` durch `ipwhitelist` ersetzen.
- Bei einem Wildcard-Zertifikat ohne Certresolver die Zeile mit
  `tls.certresolver` entfernen.
- Steht Traefik hinter einem weiteren Proxy oder NAT, sieht die Allowlist
  dessen IP. Dann `TRAEFIK_ALLOW` anpassen oder die Zeile entfernen.
- Der Login der Seite (`CME_WEB_USER`/`CME_WEB_PASS`) bleibt zusätzlich aktiv.

## Anmeldung mit Authentik / Pocket-ID (OIDC)

Die Seite kann sich per OpenID Connect bei einem oder mehreren Anbietern
anmelden. Auf der Login-Seite erscheint dann je Anbieter ein Knopf. Der lokale
Benutzer (`CME_WEB_USER`/`CME_WEB_PASS`) bleibt als Notzugang erhalten, falls er
gesetzt ist – lässt man ihn weg, geht es nur noch per OIDC.

| Variable | Beispiel | Bedeutung |
|---|---|---|
| `CME_PUBLIC_URL` | `https://cme.wiederhol.de` | Öffentliche URL, daraus wird die Redirect-URI gebildet |
| `CME_SECRET_KEY` | (lange Zufallszeichenkette) | Schlüssel für das Session-Cookie; sonst nach Neustart neu anmelden |
| `OIDC_PROVIDERS` | `authentik,pocketid` | Aktive Anbieter |
| `OIDC_AUTHENTIK_ISSUER` | `https://auth.example.de/application/o/cme/` | Issuer von Authentik |
| `OIDC_AUTHENTIK_CLIENT_ID` / `_CLIENT_SECRET` | | Aus dem Authentik-Provider |
| `OIDC_POCKETID_ISSUER` | `https://id.example.de` | Issuer von Pocket-ID |
| `OIDC_POCKETID_CLIENT_ID` / `_CLIENT_SECRET` | | Aus dem Pocket-ID-Client (Secret leer bei „Public Client“) |
| `OIDC_POCKETID_SCOPES` | `openid email profile groups` | Standard; `groups` liefert die Gruppen |
| `OIDC_ALLOWED_GROUPS` | `cme-admins` | Optional: nur Mitglieder dieser Gruppen |
| `OIDC_ALLOWED_USERS` | `thomas@example.de` | Optional: nur diese Benutzer (E-Mail oder Benutzername) |

Sind `OIDC_ALLOWED_GROUPS` und/oder `OIDC_ALLOWED_USERS` gesetzt, reicht es, wenn
eine der beiden Bedingungen passt. Ohne beide darf jeder rein, den der Anbieter
anmeldet – dann den Zugriff im Anbieter selbst einschränken.

Redirect-URIs, die im Anbieter eingetragen werden müssen:
```
https://cme.wiederhol.de/auth/callback/authentik
https://cme.wiederhol.de/auth/callback/pocketid
```
(Der letzte Teil ist der Name aus `OIDC_PROVIDERS`.) Wer zusätzlich direkt über
Port 8080 zugreift, trägt `http://<docker-host>:8080/auth/callback/...` ebenfalls
ein – oder nutzt dort den lokalen Benutzer.

### Authentik

1. **Applications → Providers → Create → OAuth2/OpenID Provider**
   - Client type: *Confidential*
   - Redirect URIs: `https://cme.wiederhol.de/auth/callback/authentik`
   - Signing Key: ein Zertifikat auswählen (z. B. „authentik Self-signed Certificate“),
     damit die ID-Tokens mit RS256 signiert werden
   - Client ID und Client Secret notieren
2. **Applications → Create**, Slug z. B. `cme`, Provider aus Schritt 1
3. Optional unter *Policy / Group / User Bindings* nur eine Gruppe zulassen
4. Issuer: `https://<authentik-host>/application/o/cme/`

Authentik schickt die Gruppen im Scope `profile` mit, `OIDC_ALLOWED_GROUPS`
funktioniert also ohne weitere Einstellung.

### Pocket-ID

1. **OIDC Clients → Add OIDC Client**, Name `CME`
2. Callback URL: `https://cme.wiederhol.de/auth/callback/pocketid`
3. Optional *Public Client* aktivieren – dann `OIDC_POCKETID_CLIENT_SECRET` leer
   lassen, die Anmeldung läuft dann nur mit PKCE
4. Optional unter *Allowed User Groups* nur bestimmte Gruppen zulassen
5. Issuer: `https://<pocket-id-host>`

Der Container muss die Issuer-URL selbst erreichen können (Discovery und
Token-Abruf laufen vom Server aus).

## Mit docker compose auf der Kommandozeile

```
cp .env.example .env     # Werte eintragen
docker compose up -d --build
```

## Ohne Docker (zum Testen auf dem Mac)

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 cme_web.py --host 192.168.1.4 --user thomas      # http://127.0.0.1:8080
.venv/bin/python3 cme_web.py --demo                                 # ohne Router
```

## Was die Seite an den Router schickt

Rufnummer anlegen (nächster freier DN-Tag):
```
voice register dn <tag>
 number <nummer>
 name <name>
 label <beschriftung>
```

Telefon anlegen (nächster freier Pool-Tag):
```
voice register pool <tag>
 id mac <mac>
 type <typ>
 number 1 dn <dn> ...
 dtmf-relay rtp-nte
 codec g711alaw
 username <benutzer> password <passwort>
 description <beschreibung>
voice register global
 create profile
```

Telefon bearbeiten: Es werden nur geänderte Werte geschickt (z. B. `type`,
`description`, `no number 2 dn 5` / `number 2 dn 7`, bei neuer MAC erst
`no id mac <alt>`), danach `create profile` und optional `restart` für den Pool.

Rufnummer bearbeiten: `number`, `name`/`no name`, `label`/`no label` und
Shared Line; danach `create profile` und optional `restart` für alle Telefone,
auf denen die Nummer liegt.

Telefon löschen: `no voice register pool <tag>`, danach `create profile`. Die
Rufnummern des Telefons bleiben erhalten.

Rufnummer löschen: Hängt die Nummer noch an Telefonen, wird sie dort zuerst mit
`no number <n> dn <tag>` entfernt (die Seite fragt vorher nach), dann
`no voice register dn <tag>`, `create profile` und `restart` der betroffenen Telefone.

### Fremdgeräte (z. B. Gigaset-Basisstation)

Im CME-Modus erkennt der Router **alle** Telefone nur an der MAC-Adresse, auch
Fremdgeräte (`id ip …` lehnt er mit „CME mode only accept mac address“ ab). Für eine
Gigaset-Basis also die MAC der Basis eintragen (Aufkleber oder Gigaset-Weboberfläche)
und als Typ „Fremdgerät (ohne Typ)“ wählen:
```
voice register pool <tag>
 id mac 7C2F.8011.2233
 number 1 dn <dn>
 number 2 dn <dn> ...
 dtmf-relay rtp-nte
 codec g711alaw
 username <benutzer> password <passwort>
 description Gigaset
```
CME verlangt für das SIP-Passwort 6–15 Buchstaben/Ziffern ohne Sonderzeichen, mit
mindestens einer Ziffer, einem Groß- und einem Kleinbuchstaben. Ein leer gelassenes
Passwort erzeugt die Seite passend dazu. Am Gerät je Mobilteil bzw.
VoIP-Verbindung eintragen:

| Gigaset | Wert |
|---|---|
| Domain / Registrar / Proxy | IP des Routers, Port 5060 |
| Benutzername (SIP-User) | die Rufnummer der Leitung, z. B. `110` |
| Authentifizierungsname | SIP-Benutzer aus dem Formular |
| Passwort | SIP-Passwort aus dem Formular |

Mehrere Mobilteile: je Mobilteil eine Rufnummer anlegen und alle als Leitungen
desselben Pools eintragen; sie teilen sich Authentifizierungsname und Passwort.

Damit CME die Anmeldedaten auch wirklich prüft, sollte unter
`voice register global` der Befehl `authenticate register` gesetzt sein.

### Codec-Listen

In der Karte **Codec-Listen** werden `voice class codec`-Listen angelegt, sortiert,
bearbeitet und gelöscht:
```
voice class codec <tag>
 codec preference 1 g722-64
 codec preference 2 g711alaw
 codec preference 3 g711ulaw
```
Beim Telefon wählt man unter *Codec* entweder einen einzelnen Codec (`codec …`)
oder eine Liste (`voice-class codec <tag>`). Die Tabelle zeigt, wo eine Liste
verwendet wird – auch von Dial-Peers, etwa dem SIP-Trunk. Wird eine solche Liste
geändert, wirkt das dort sofort; die Seite weist darauf hin. Beim Bearbeiten
werden erst alle alten Einträge entfernt (`no codec preference n`) und dann die
neuen gesetzt. Gelöscht werden kann eine Liste nur, wenn sie nirgends verwendet wird.

Welche Codecs der Router für Listen kennt (z. B. `opus`, `ilbc`), hängt von der
IOS-Version ab; ein unbekannter Codec wird vom Router mit einer `%`-Meldung abgelehnt.

### Sammelgruppen (Hunt Groups)

In der Karte **Sammelgruppen** werden `voice hunt-group`s angelegt, bearbeitet und
gelöscht. Eine Sammelgruppe hat eine eigene **Pilotnummer**; ruft jemand sie an,
klingeln die Mitglieder (Rufnummern/DNs):
```
voice hunt-group <tag> parallel
 pilot +4961874191283
 list 2831,2832
 timeout 30
 final 2831
 name Testnummer
```

| Art | Verhalten |
|---|---|
| Parallel | alle Mitglieder gleichzeitig |
| Nacheinander (`sequential`) | in Listenreihenfolge, immer ab dem ersten |
| Reihum (`peer`) | ab dem Mitglied nach dem zuletzt angerufenen |
| Längste Ruhezeit (`longest-idle`) | zuerst das Mitglied, das am längsten keinen Anruf hatte |

Hinweise:
- Die Pilotnummer darf keine vorhandene Rufnummer (DN) sein. Soll eine externe
  Nummer auf mehreren Geräten klingeln, bekommt jedes Gerät eine eigene interne
  Nummer und die externe Nummer wird Pilot einer parallelen Gruppe. Das ist auch
  der Weg für Fremdgeräte wie Gigaset, die bei Shared Lines nicht mitklingeln.
- Die Art lässt sich in IOS nicht ändern; die Seite löscht die Gruppe dafür und
  legt sie neu an (`no voice hunt-group <tag>`).
- Wird die Nummer eines Mitglieds geändert, passt die Seite die Liste der Gruppe an.
  Eine Rufnummer, die noch Mitglied ist, lässt sich nicht löschen.
- Ausgehende Anrufe zeigen die interne Nummer des Geräts. Soll nach außen die
  Pilotnummer erscheinen, braucht der ausgehende Dial-Peer eine Übersetzungsregel
  (`translation-profile outgoing`).

### Shared Lines

Eine Rufnummer, die auf mehreren Telefonen klingeln soll, wird im Formular
„Rufnummer anlegen/bearbeiten“ als **Shared Line** markiert:
```
voice register dn <tag>
 shared-line [max-calls <n>]
```
Erst dann lässt sie sich in „Telefon anlegen/bearbeiten“ mehreren Telefonen als
Leitung zuordnen. Nummern ohne Shared Line, die schon an einem anderen Telefon
hängen, sind in der Auswahl gesperrt. Shared Line ausschalten geht erst, wenn die
Nummer nur noch auf einem Telefon liegt.

Geänderte Konfiguration wird erst mit „Konfiguration speichern“ (`write memory`)
dauerhaft gespeichert.

## Abfragen für den Status

- `show sip-ua register status` – Registrierung beim Provider
- `show dial-peer voice summary` – Dial-Peers inkl. SIP-Options-Keepalive
- `show voice register pool all brief` – registrierte Telefone
- `show running-config | section voice register` – Rufnummern und Pools

## Sicherheit

- Ohne OIDC und ohne `CME_WEB_USER`/`CME_WEB_PASS` ist die Seite ohne Login
  erreichbar – im Container daher immer eines davon setzen.
- Anmeldung über ein Session-Cookie (HttpOnly, SameSite=Lax, bei https auch
  Secure). Ändernde API-Aufrufe werden nur als JSON angenommen (Schutz gegen CSRF).
- Formulareingaben werden streng geprüft, damit keine fremden Befehle in die
  Router-Konfiguration gelangen.
- In der Rohausgabe sind nur fest hinterlegte `show`-Befehle erlaubt.
