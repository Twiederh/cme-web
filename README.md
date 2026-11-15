# CME Web – Weboberfläche für Cisco Call Manager Express (SIP)

Zeigt den Status der SIP-Trunks, Dial-Peers, SIP-Telefone und Rufnummern
und legt Rufnummern (`voice register dn`) und Telefone (`voice register pool`) an.
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

Geänderte Konfiguration wird erst mit „Konfiguration speichern“ (`write memory`)
dauerhaft gespeichert.

## Abfragen für den Status

- `show sip-ua register status` – Registrierung beim Provider
- `show dial-peer voice summary` – Dial-Peers inkl. SIP-Options-Keepalive
- `show voice register pool all brief` – registrierte Telefone
- `show running-config | section voice register` – Rufnummern und Pools

## Sicherheit

- Ohne `CME_WEB_USER`/`CME_WEB_PASS` ist die Seite ohne Login erreichbar – im
  Container daher immer setzen.
- Formulareingaben werden streng geprüft, damit keine fremden Befehle in die
  Router-Konfiguration gelangen.
- In der Rohausgabe sind nur fest hinterlegte `show`-Befehle erlaubt.
