"""
jitsi.py - Konferenzen in Jitsi Meet über Jigasi, angebunden an Kamailio (Tab "Jitsi", nur Administratoren)

Aufbau:
  CME --Rufnummer eines Raums--> Kamailio (VM_HOST_IP:CONF_PORT) --Jitsi-Conference-Room--> Jigasi --> Raum in Jitsi
  Jigasi ("Telefon einladen" in Jitsi) --> Kamailio (CONF_PORT) --> CME (Dial-Peer "incoming uri via" auf CONF_PORT)
  Von draußen (Kamailio WITH_EXT): <name>@SIP_PUBLIC_DOMAIN --> Jigasi, wenn der Raum einen Namen hat

cme-web
  * liest und ändert die Jigasi-Konfiguration per SSH (eigener Schlüssel, sudo ohne Passwort auf dem Jitsi-Server):
    /etc/jitsi/jigasi/sip-communicator.properties, vor jeder Änderung eine Sicherung daneben, danach Neustart von Jigasi
  * schreibt die Räume für Kamailio nach PHONEAPP_DATA_DIR/conferences (kamailio/passwd2htable.py --conf)
  * schreibt die Dial-Peers der CME (je Raum eine Rufnummer, dazu einer für die Anrufe von Jigasi)
  * übernimmt auf Wunsch das Let's-Encrypt-Zertifikat des Jitsi-Servers für Prosody (Renewal-Hook)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent

PROPS = "/etc/jitsi/jigasi/sip-communicator.properties"
SIP_ACC = "net.java.sip.communicator.impl.protocol.sip."
XMPP_ACC = "net.java.sip.communicator.impl.protocol.jabber.acc-xmpp-1."
K_ROOM = "org.jitsi.jigasi.DEFAULT_JVB_ROOM_NAME"
K_MUC = "org.jitsi.jigasi.MUC_SERVICE_ADDRESS"
K_TRUST = "net.java.sip.communicator.service.gui.ALWAYS_TRUST_MODE_ENABLED"
K_XUSER = "org.jitsi.jigasi.xmpp.acc.USER_ID"
K_XPASS = "org.jitsi.jigasi.xmpp.acc.PASS"
K_XANON = "org.jitsi.jigasi.xmpp.acc.ANONYMOUS_AUTH"
K_SIPPORT = "org.jitsi.jigasi.SIP_PORT"
CODECS = ["opus/48000", "G722/8000", "PCMA/8000", "PCMU/8000"]
PEER_DESC = "Jitsi-Konferenz"
OUT_DESC = "Jitsi-Konferenz: Jigasi waehlt hinaus"
URI_CLASS = "JITSI"
RE_HOST = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
RE_DOMAIN = re.compile(r"[a-z0-9][a-z0-9.-]{0,200}")
RE_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
RE_NUM = re.compile(r"^[0-9*#]{2,32}$")
RE_ROOM = re.compile(r"^[a-z0-9._-]{1,80}(@[a-z0-9.-]{1,120})?$")
RE_NAME = re.compile(r"^[a-z0-9._+-]{1,64}$")
RE_SIPUSER = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


class JitsiError(ValueError):
    pass


# ---------------- sip-communicator.properties ----------------

def parse_props(text: str) -> dict[str, str]:
    """Schlüssel -> Wert (ohne Kommentare). Fortsetzungszeilen kommen in Jigasis Datei nicht vor.
    Ein leerer Wert überschreibt keinen früheren: Die Beispieldatei von Jigasi hat z.B. acc-xmpp-1.PASSWORD
    oben mit dem Passwort des Installers und weiter unten noch einmal leer."""
    out = {}
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith(("#", "!")) or "=" not in t:
            continue
        k, _, v = t.partition("=")
        k, v = k.strip(), v.strip()
        if v or k not in out:
            out[k] = v
    return out


def set_props(text: str, changes: dict[str, str | None]) -> str:
    """Werte setzen (None = Zeile entfernen); vorhandene Zeilen an ihrer Stelle ändern, neue anhängen."""
    lines = text.splitlines()
    done = set()
    out = []
    for line in lines:
        t = line.strip()
        k = t.partition("=")[0].strip() if "=" in t and not t.startswith(("#", "!")) else None
        if k in changes:
            if k in done or changes[k] is None:
                continue                      # doppelt oder entfernen
            out.append(f"{k}={changes[k]}")
            done.add(k)
        else:
            out.append(line)
    new = [f"{k}={v}" for k, v in changes.items() if v is not None and k not in done]
    if new:
        out += ["", "# von cme-web (Tab Jitsi)"] + new
    return "\n".join(out) + "\n"


def sip_account(props: dict[str, str]) -> str:
    """Kennung des (ersten) SIP-Kontos, z. B. acc1403273890647"""
    for k, v in props.items():
        m = re.fullmatch(re.escape(SIP_ACC) + r"(acc[0-9A-Za-z-]+)", k)
        if m and v == m.group(1):
            return m.group(1)
    raise JitsiError("Kein SIP-Konto in der Jigasi-Konfiguration gefunden.")


def model(props: dict[str, str], host_ip: str, conf_port: int) -> dict:
    acc = sip_account(props)
    p = SIP_ACC + acc + "."
    g = lambda k, d="": props.get(p + k, d)  # noqa: E731
    muc = props.get(K_MUC, "")
    domain = props.get(XMPP_ACC + "DOMAIN_BASE", "") or (muc.split(".", 1)[1] if muc.count(".") > 1 else "")
    enc = {c: int(g("Encodings." + c, "0") or 0) for c in CODECS}
    proxy, pport = g("PROXY_ADDRESS"), g("PROXY_PORT")
    return {
        "account": acc, "user_id": g("USER_ID"), "server": g("SERVER_ADDRESS"), "proxy": proxy, "proxy_port": pport,
        "transport": g("PREFERRED_TRANSPORT", "UDP"), "password_set": bool(g("PASSWORD")), "encodings": enc,
        "default_room": props.get(K_ROOM, ""), "muc": muc, "domain": domain,
        "trust_all": props.get(K_TRUST, "false").lower() == "true",
        "xmpp_login": props.get(K_XANON, "true").lower() == "false" and bool(props.get(K_XUSER)),
        "xmpp_user": props.get(K_XUSER, ""), "brewery_user": props.get(XMPP_ACC + "USER_ID", ""),
        "sip_port": int(props.get(K_SIPPORT, "") or props.get("net.java.sip.communicator.SIP_PREFERRED_CLEAR_PORT", "")
                        or 5060),
        "via_kamailio": proxy == host_ip and str(pport) == str(conf_port) and not g("SERVER_ADDRESS"),
    }


def changes_for(props: dict[str, str], form: dict, host_ip: str, conf_port: int) -> dict[str, str | None]:
    """Formular -> Änderungen an der Datei. form["kamailio"]: Jigasi ohne Registrar über Kamailio."""
    acc = sip_account(props)
    p = SIP_ACC + acc + "."
    ch: dict[str, str | None] = {}
    if form.get("kamailio"):
        ch.update({p + "PROXY_ADDRESS": host_ip, p + "PROXY_PORT": str(conf_port), p + "IS_PROXY_OVERRIDDEN": "true",
                   p + "PREFERRED_TRANSPORT": "UDP", p + "SERVER_ADDRESS": None})
    else:
        if "proxy" in form:
            proxy = str(form["proxy"]).strip()
            if proxy and not RE_HOST.match(proxy):
                raise JitsiError("Proxy: ungültige Adresse.")
            ch[p + "PROXY_ADDRESS"] = proxy or None
            ch[p + "IS_PROXY_OVERRIDDEN"] = "true" if proxy else None
        if "proxy_port" in form:
            port = str(form["proxy_port"]).strip()
            if port and not (port.isdigit() and 0 < int(port) < 65536):
                raise JitsiError("Proxy-Port: 1–65535.")
            ch[p + "PROXY_PORT"] = port or None
        if "server" in form:
            server = str(form["server"]).strip()
            if server and not RE_HOST.match(server):
                raise JitsiError("Registrar: ungültige Adresse.")
            ch[p + "SERVER_ADDRESS"] = server or None
        if "transport" in form:
            t = str(form["transport"]).upper()
            if t not in ("UDP", "TCP", "TLS"):
                raise JitsiError("Transport: UDP, TCP oder TLS.")
            ch[p + "PREFERRED_TRANSPORT"] = t
    if "user_id" in form:
        u = str(form["user_id"]).strip()
        if not RE_SIPUSER.match(u):
            raise JitsiError("SIP-Benutzer: Buchstaben, Ziffern, . _ + -")
        ch[p + "USER_ID"] = u
    if isinstance(form.get("encodings"), dict):
        for c in CODECS:
            if c in form["encodings"]:
                v = int(form["encodings"][c] or 0)
                if not 0 <= v <= 1000:
                    raise JitsiError("Codec-Priorität: 0–1000 (0 = aus).")
                ch[p + "Encodings." + c] = str(v)
        if not any(int(form["encodings"].get(c, 0) or 0) for c in CODECS):
            raise JitsiError("Mindestens ein Codec muss an sein.")
        ch[p + "OVERRIDE_ENCODINGS"] = "true"
    if "default_room" in form:
        r = str(form["default_room"]).strip().lower()
        if r and not RE_ROOM.match(r):
            raise JitsiError("Standard-Raum: Kleinbuchstaben, Ziffern, . _ -")
        muc = props.get(K_MUC, "")
        ch[K_ROOM] = (r if "@" in r or not muc else f"{r}@{muc}") if r else None
    if "trust_all" in form:
        ch[K_TRUST] = "true" if form["trust_all"] else None
    if "xmpp_login" in form:
        if form["xmpp_login"]:
            user, pw = props.get(XMPP_ACC + "USER_ID", ""), props.get(XMPP_ACC + "PASSWORD", "")
            if not user or not pw:
                raise JitsiError("Kein Jigasi-Konto (acc-xmpp-1) mit Passwort in der Konfiguration.")
            ch.update({K_XUSER: user, K_XPASS: pw, K_XANON: "false"})
        else:
            ch.update({K_XUSER: None, K_XPASS: None, K_XANON: None})
    return ch


# ---------------- Räume, Kamailio, CME ----------------

def room_jid(room: str, muc: str) -> str:
    return room if "@" in room or not muc else f"{room}@{muc}"


def check_rooms(rooms: list) -> list[dict]:
    out, nums, names = [], set(), set()
    for r in rooms or []:
        num = str(r.get("number", "")).strip()
        room = str(r.get("room", "")).strip().lower()
        name = str(r.get("name", "")).strip().lower()
        if not num and not name:
            continue
        if num and not RE_NUM.match(num):
            raise JitsiError(f"Rufnummer „{num}“: 2–32 Ziffern.")
        if not RE_ROOM.match(room):
            raise JitsiError(f"Raum „{room}“: Kleinbuchstaben, Ziffern, . _ - (wie in der Jitsi-Adresse).")
        if name and not RE_NAME.match(name):
            raise JitsiError(f"Name „{name}“: Kleinbuchstaben, Ziffern, . _ + -")
        if num in nums or (name and name in names):
            raise JitsiError(f"„{num or name}“ ist doppelt.")
        nums.add(num)
        names.add(name)
        out.append({"number": num, "room": room, "name": name})
    return out


def conf_file(jigasi_ip: str, sip_port: int, rooms: list[dict], muc: str) -> str:
    lines = ["version:1"]
    if jigasi_ip:
        lines += [f"@ip {jigasi_ip}", f"@port {sip_port}"]
    for r in rooms:
        jid = room_jid(r["room"], muc)
        if r["number"]:
            lines.append(f"{r['number']} {jid}")
        if r["name"]:
            lines.append(f"={r['name']} {jid}")
    return "\n".join(lines) + "\n"


def parse_peers(cfg: str) -> dict[str, dict]:
    """dial-peer voice <tag> voip -> {desc, pattern, target, lines}"""
    peers, cur = {}, None
    for line in cfg.splitlines():
        m = re.match(r"^dial-peer voice (\d+) voip", line)
        if m:
            cur = peers.setdefault(m.group(1), {"desc": "", "pattern": "", "target": "", "lines": []})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        cur["lines"].append(t)
        if t.startswith("description "):
            cur["desc"] = t[12:]
        elif t.startswith("destination-pattern "):
            cur["pattern"] = t[20:]
        elif t.startswith("session target "):
            cur["target"] = t[15:]
    return peers


def cme_lines(rooms: list[dict], cfg: str, host_ip: str, conf_port: int, jitsi_ip: str, dialout: bool,
              codec: str = "3") -> list[str]:
    """Dial-Peers: je Rufnummer einer zu Kamailio (alte direkt zu Jitsi werden übernommen), einer für Jigasi -> CME."""
    peers = parse_peers(cfg)
    used = {int(t) for t in peers}
    ours = {t: p for t, p in peers.items() if p["desc"].startswith(PEER_DESC)
            or (jitsi_ip and p["target"].startswith(f"ipv4:{jitsi_ip}"))}
    by_pat = {p["pattern"]: t for t, p in ours.items() if p["pattern"]}

    def free(start: int) -> str:
        t = next(t for t in range(start, start + 1000) if t not in used)
        used.add(t)
        return str(t)

    lines, keep = [], set()
    for r in rooms:
        if not r["number"]:
            continue
        tag = by_pat.get(r["number"]) or free(8800)
        keep.add(tag)
        old = peers.get(tag, {"lines": []})["lines"]
        lines += [f"dial-peer voice {tag} voip", f" description {PEER_DESC} {r['room']}"[:80],
                  f" destination-pattern {r['number']}", " session protocol sipv2",
                  f" session target ipv4:{host_ip}:{conf_port}", " session transport udp",
                  f" voice-class codec {codec}", " dtmf-relay rtp-nte", " no vad"]
        # alte Bindung an eine Schnittstelle (direkt zu Jitsi) gilt für Kamailio nicht
        lines += [f" no {x}" for x in old if x.startswith("voice-class sip bind ")]
    out_tag = next((t for t, p in ours.items() if p["desc"] == OUT_DESC), "")
    for t, p in ours.items():
        if t not in keep and t != out_tag:
            lines.append(f"no dial-peer voice {t} voip")
    pat = re.escape(host_ip) + f":{conf_port}"
    if dialout:
        tag = out_tag or free(8890)
        lines += [f"voice class uri {URI_CLASS} sip", f" pattern {pat}",
                  f"dial-peer voice {tag} voip", f" description {OUT_DESC}", " session protocol sipv2",
                  " session transport udp", f" incoming uri via {URI_CLASS}", f" voice-class codec {codec}",
                  " dtmf-relay rtp-nte", " no vad"]
    elif out_tag:
        lines += [f"no dial-peer voice {out_tag} voip", f"no voice class uri {URI_CLASS}"]
    return lines


def prosody_hook(le_dir: str, hosts: list[str]) -> str:
    body = "".join(f'  rm -f "/etc/prosody/certs/{h}.crt" "/etc/prosody/certs/{h}.key"\n'
                   f'  install -m 640 -o root -g prosody "$L/fullchain.pem" "/etc/prosody/certs/{h}.crt"\n'
                   f'  install -m 640 -o root -g prosody "$L/privkey.pem" "/etc/prosody/certs/{h}.key"\n'
                   for h in hosts)
    return ("#!/bin/sh\n# von cme-web (Tab Jitsi): Let's-Encrypt-Zertifikat auch für Prosody (XMPP), danach Neustart\n"
            f'L="{le_dir}"\n[ -f "$L/fullchain.pem" ] || exit 0\n{body}'
            "systemctl restart prosody\nsleep 3\nsystemctl restart jicofo jitsi-videobridge2 jigasi 2>/dev/null || true\n")


def prosody_allow_script(domain: str, jid: str) -> str:
    """sh-Skript (als root): Jigasi-Konto in token_verification_allowlist der Konferenz-Komponente eintragen.
    Mit Sicherung, Prüfung der Konfiguration und Neustart von Prosody, Jicofo, Videobridge und Jigasi.
    Ausgabe: ALLOW=schon bzw. ALLOW=neu."""
    if not RE_DOMAIN.fullmatch(domain) or not re.fullmatch(r"[a-z0-9._-]{1,64}@[a-z0-9.-]{1,200}", jid):
        raise JitsiError("Domain oder Jigasi-Konto ungültig.")
    comp = re.escape(f"conference.{domain}")
    return f"""set -e
F=/etc/prosody/conf.d/{domain}.cfg.lua
test -f "$F" || {{ echo "Keine Prosody-Konfiguration $F"; exit 1; }}
grep -q '^Component "conference.{domain}" "muc"' "$F" || {{ echo "Komponente conference.{domain} fehlt in $F"; exit 1; }}
if awk '/^Component /{{c=0}} /^Component "{comp}" "muc"/{{c=1}} c && /token_verification_allowlist/ && /"{re.escape(jid)}"/{{f=1}} END{{exit !f}}' "$F"; then
  echo ALLOW=schon; exit 0
fi
cp -p "$F" "$F.cme-web-$(date +%Y%m%d-%H%M%S)"
# Liste schon da (ohne Jigasi): ergänzen, sonst neue Zeile nach admins
HAS=$(awk '/^Component /{{c=0}} /^Component "{comp}" "muc"/{{c=1}} c && /token_verification_allowlist/{{print 1}}' "$F")
awk -v jid='{jid}' -v has="$HAS" '
  /^Component /{{c=0}}
  /^Component "{comp}" "muc"/{{c=1; print; next}}
  c && /token_verification_allowlist *= *[{{]/{{sub(/[{{]/, "{{ \\"" jid "\\",") ; print; c=0; done=1; next}}
  c && has == "" && /^ *admins *=/{{print; print "    -- cme-web (Tab Jitsi): Jigasi darf ohne Token in die Räume"; print "    token_verification_allowlist = {{ \\"" jid "\\" }}"; c=0; done=1; next}}
  {{print}}
  END{{if (!done) exit 3}}' "$F" > "$F.new" || {{ rm -f "$F.new"; echo "Keine Stelle für den Eintrag gefunden (admins der Komponente)"; exit 1; }}
chmod --reference="$F" "$F.new"; chown --reference="$F" "$F.new"; mv "$F.new" "$F"
prosodyctl check config >/dev/null 2>&1 || true
systemctl restart prosody
sleep 3
systemctl restart jicofo jitsi-videobridge2 jigasi 2>/dev/null || true
echo ALLOW=neu
"""


def jicofo_trust_script(auth_domain: str) -> str:
    """sh-Skript (als root): Domain des Jigasi-Kontos in jicofo.xmpp.trusted-domains, damit Jicofo die Konferenz auch
    für Jigasi ohne Token eröffnet (Gäste warten dann wegen muc_wait_for_host in der Lobby). Ausgabe TRUST=schon/neu."""
    if not RE_DOMAIN.fullmatch(auth_domain):
        raise JitsiError("Domain des Jigasi-Kontos ungültig.")
    return f"""set -e
F=/etc/jitsi/jicofo/jicofo.conf
test -f "$F" || {{ echo "Keine Jicofo-Konfiguration $F"; exit 1; }}
if grep -q 'trusted-domains:.*"{auth_domain}"' "$F"; then echo TRUST=schon; exit 0; fi
cp -p "$F" "$F.cme-web-$(date +%Y%m%d-%H%M%S)"
if grep -q 'trusted-domains: *\\[' "$F"; then
  sed -i.cme 's/trusted-domains: *\\[/trusted-domains: [ "{auth_domain}",/' "$F" && rm -f "$F.cme"
else
  sed -i '0,/^ *xmpp: *{{/s//  xmpp: {{\\n    trusted-domains: [ "{auth_domain}" ]/' "$F"
fi
systemctl restart jicofo
echo TRUST=neu
"""


# ---------------- Flask ----------------

def register(app, router, apply, demo: bool, cfgdir: Path) -> None:
    store = cfgdir / "cme-jitsi.json"
    key_path = cfgdir / "cme-jitsi-ssh"
    known = cfgdir / "cme-jitsi-known_hosts"
    data = Path(os.environ.get("PHONEAPP_DATA_DIR", "/data/push"))
    if demo:
        import tempfile
        data = Path(tempfile.gettempdir()) / "cme-web-demo-push"
    conf_path = data / "conferences"
    host_ip = os.environ.get("VM_HOST_IP", "").strip()
    conf_port = int(os.environ.get("CONF_PORT", "5068") or 5068)
    lock = threading.Lock()

    def settings() -> dict:
        s = {"host": "", "user": "thomas", "port": 22, "rooms": [], "dialout": True, "codec": "3", "muc": "",
             "jigasi_ip": "", "sip_port": 5060}
        try:
            s.update(json.loads(store.read_text()))
        except (FileNotFoundError, ValueError):
            pass
        return s

    def save(s: dict) -> None:
        store.write_text(json.dumps(s, indent=1))
        os.chmod(store, 0o600)

    # ---- SSH ----
    def pubkey() -> str:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if not key_path.exists():
            k = Ed25519PrivateKey.generate()
            key_path.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                 serialization.NoEncryption()))
            os.chmod(key_path, 0o600)
        k = serialization.load_ssh_private_key(key_path.read_bytes(), None)
        return k.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH).decode() + " cme-web"

    def ssh(cmd: str, stdin: str | None = None, timeout: int = 30) -> str:
        import paramiko
        s = settings()
        if not s["host"]:
            raise JitsiError("Erst den Jitsi-Server eintragen.")
        pubkey()
        c = paramiko.SSHClient()
        if known.exists():
            c.load_host_keys(str(known))
        # erster Kontakt: Schlüssel des Servers merken (danach muss er gleich bleiben)
        c.set_missing_host_key_policy(paramiko.RejectPolicy() if known.exists() and known.read_text().strip()
                                      else paramiko.AutoAddPolicy())
        try:
            c.connect(s["host"], port=int(s["port"]), username=s["user"],
                      pkey=paramiko.Ed25519Key.from_private_key_file(str(key_path)),
                      timeout=10, allow_agent=False, look_for_keys=False)
        except paramiko.AuthenticationException:
            raise JitsiError(f"SSH: Anmeldung als {s['user']}@{s['host']} abgelehnt – den Schlüssel unten in "
                             f"~{s['user']}/.ssh/authorized_keys auf {s['host']} eintragen.") from None
        except paramiko.SSHException as e:
            raise JitsiError(f"SSH: {e}") from None
        except OSError as e:
            raise JitsiError(f"SSH: {s['host']} nicht erreichbar ({e})") from None
        try:
            c.save_host_keys(str(known))
            i, o, e = c.exec_command(cmd, timeout=timeout)
            if stdin is not None:
                i.write(stdin)
                i.channel.shutdown_write()
            out, err = o.read().decode(errors="replace"), e.read().decode(errors="replace")
            rc = o.channel.recv_exit_status()
        finally:
            c.close()
        if rc != 0:
            msg = err.strip() or out.strip() or f"Exit {rc}"
            if "sudo" in msg and ("password" in msg.lower() or "passwort" in msg.lower()):
                msg = f"sudo verlangt ein Passwort – für {s['user']} sudo ohne Passwort erlauben ({msg})"
            raise JitsiError(msg[-600:])
        return out

    def read_props() -> str:
        return ssh(f"sudo -n cat {PROPS}")

    def write_props(text: str) -> None:
        ssh(f"sudo -n sh -c 'F={PROPS}; cp -p \"$F\" \"$F.cme-web-$(date +%Y%m%d-%H%M%S)\" && "
            "cat > \"$F.new\" && chown --reference=\"$F\" \"$F.new\" && chmod 600 \"$F.new\" && mv \"$F.new\" \"$F\" && "
            "ls -1t \"$F\".cme-web-* | tail -n +6 | xargs -r rm -f && systemctl restart jigasi'", stdin=text)

    def write_conf(s: dict) -> None:
        text = conf_file(s.get("jigasi_ip", ""), int(s.get("sip_port") or 5060), s["rooms"], s.get("muc", ""))
        data.mkdir(parents=True, exist_ok=True)
        tmp = conf_path.with_suffix(".tmp")
        tmp.write_text(text)
        os.chmod(tmp, 0o644)
        tmp.replace(conf_path)

    def resolve(host: str) -> str:
        try:
            return socket.gethostbyname(host)
        except OSError:
            return host if re.fullmatch(r"[0-9.]+", host) else ""

    def remember(s: dict, m: dict) -> None:
        """Aus der Jigasi-Konfiguration, was Kamailio braucht (MUC, SIP-Port, IP)"""
        s.update(muc=m["muc"], sip_port=m["sip_port"], jigasi_ip=resolve(s["host"]))
        save(s)
        write_conf(s)

    @app.errorhandler(JitsiError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/jitsi.js")
    def jitsi_js():
        return send_file(BASE / "static" / "jitsi.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/jitsi")
    def jitsi_status():
        s = settings()
        out = {"ok": True, "settings": {k: s[k] for k in ("host", "user", "port", "rooms", "dialout", "codec")},
               "pubkey": pubkey(), "host_ip": host_ip, "conf_port": conf_port,
               "public_domain": os.environ.get("SIP_PUBLIC_DOMAIN", "").strip(), "jigasi": None, "error": None}
        peers = parse_peers(router.show("show running-config | section dial-peer voice"))
        out["cme_peers"] = [{"tag": t, **{k: p[k] for k in ("desc", "pattern", "target")}} for t, p in sorted(peers.items())
                            if p["desc"].startswith(PEER_DESC) or (s.get("jigasi_ip") and s["jigasi_ip"] in p["target"])
                            or (s["host"] and s["host"] in p["target"])]
        if not s["host"]:
            return jsonify(out)
        try:
            with lock:
                props = parse_props(read_props())
                m = model(props, host_ip, conf_port)
                remember(s, m)
                st = ssh("systemctl is-active jigasi prosody jicofo jitsi-videobridge2; "
                         f"sudo -n openssl x509 -in /etc/prosody/certs/{m['domain']}.crt -noout -issuer -enddate 2>&1; "
                         f"sudo -n test -f /etc/letsencrypt/live/{m['domain']}/fullchain.pem && echo LE=1 || echo LE=0; "
                         "sudo -n test -f /etc/letsencrypt/renewal-hooks/deploy/prosody-cme-web.sh && echo HOOK=1 || echo HOOK=0; "
                         f"sudo -n grep -m1 -A3 '^VirtualHost \"{m['domain']}\"' /etc/prosody/conf.d/{m['domain']}.cfg.lua "
                         "| grep -o 'authentication = \"[a-z_]*\"' || true; "
                         f"sudo -n awk '/^Component /{{c=0}} /^Component \"conference.{m['domain']}\" \"muc\"/{{c=1}} "
                         "c && /\"token_verification\"/{print \"TV=1\"} c && /token_verification_allowlist/{print \"AL=\" $0}' "
                         f"/etc/prosody/conf.d/{m['domain']}.cfg.lua 2>/dev/null || true; "
                         "sudo -n grep -h 'trusted-domains' /etc/jitsi/jicofo/jicofo.conf 2>/dev/null | sed 's/^/TD=/' || true")
            lines = st.splitlines()
            m["services"] = dict(zip(["jigasi", "prosody", "jicofo", "videobridge"], lines[:4]))
            info = "\n".join(lines[4:])
            m["prosody_cert"] = {"issuer": (re.search(r"issuer=.*?(?:O\s*=\s*([^,\n]+))", info) or [None, ""])[1].strip(),
                                 "until": (re.search(r"notAfter=(.*)", info) or [None, ""])[1].strip(),
                                 "letsencrypt": "LE=1" in info, "hook": "HOOK=1" in info}
            m["auth"] = (re.search(r'authentication = "([a-z_]+)"', info) or [None, ""])[1]
            # Konferenz-Komponente mit token_verification: darf Jigasi ohne Token in die Räume?
            jid = props.get(XMPP_ACC + "USER_ID", "").lower()
            al = "\n".join(l for l in lines if l.startswith("AL="))
            m["token_rooms"] = {"verification": "TV=1" in info, "jigasi": jid,
                                "allowed": bool(jid) and f'"{jid}"' in al.lower(),
                                "trusted": bool(jid) and any(f'"{jid.split("@")[-1]}"' in l.lower()
                                                             for l in lines if l.startswith("TD="))}
            out["jigasi"] = m
        except JitsiError as e:
            out["error"] = str(e)
        return jsonify(out)

    @app.post("/api/jitsi/settings")
    def jitsi_settings():
        d = request.get_json(force=True) or {}
        s = settings()
        host, user = str(d.get("host", s["host"])).strip(), str(d.get("user", s["user"])).strip()
        port = int(d.get("port", s["port"]) or 22)
        if host and not RE_HOST.match(host):
            raise JitsiError("Server: ungültige Adresse.")
        if not RE_USER.match(user):
            raise JitsiError("SSH-Benutzer: ungültig.")
        if host != s["host"]:
            known.unlink(missing_ok=True)      # anderer Server: dessen Schlüssel neu lernen
        s.update(host=host, user=user, port=port)
        save(s)
        return jsonify(ok=True)

    @app.get("/api/jitsi/log")
    def jitsi_log():
        n = min(int(request.args.get("n", 80)), 500)
        return jsonify(ok=True, log=ssh(f"sudo -n tail -n {n} /var/log/jitsi/jigasi.log | cut -c1-400"))

    @app.post("/api/jitsi/jigasi")
    def jitsi_jigasi():
        d = request.get_json(force=True) or {}
        with lock:
            text = read_props()
            props = parse_props(text)
            ch = changes_for(props, d, host_ip, conf_port)
            new = set_props(text, ch)
            if new != text:
                write_props(new)
            m = model(parse_props(new), host_ip, conf_port)
            remember(settings(), m)
        return jsonify(ok=True, changed=new != text, restarted=new != text)

    @app.post("/api/jitsi/jigasi-password")
    def jitsi_jigasi_password():
        """Neues Passwort für das Jigasi-Konto (acc-xmpp-1, z.B. jigasi@auth.<domain>): in Prosody setzen und in
        Jigasis Datei eintragen (acc-xmpp-1.PASSWORD, bei Anmeldung mit dem Konto auch xmpp.acc.PASS). Das Passwort
        wird zufällig erzeugt und geht nur per SSH (stdin) an den Server, nie in die Oberfläche."""
        with lock:
            text = read_props()
            props = parse_props(text)
            user = props.get(XMPP_ACC + "USER_ID", "")
            m = re.fullmatch(r"([a-z0-9._-]{1,64})@([a-z0-9.-]{1,200})", user.lower())
            if not m:
                raise JitsiError("Kein Jigasi-Konto (acc-xmpp-1.USER_ID) in der Konfiguration.")
            pw = secrets.token_urlsafe(18)     # 24 Zeichen aus A-Z a-z 0-9 - _
            ssh(f"sudo -n sh -c 'prosodyctl register \"$0\" \"$1\" \"$(cat)\"' {m.group(1)} {m.group(2)}", stdin=pw)
            ch = {XMPP_ACC + "PASSWORD": pw}   # set_props entfernt dabei den leeren Doppeleintrag
            if props.get(K_XUSER):
                ch[K_XPASS] = pw
            write_props(set_props(text, ch))
        return jsonify(ok=True, user=user)

    @app.post("/api/jitsi/rooms")
    def jitsi_rooms():
        d = request.get_json(force=True) or {}
        s = settings()
        rooms = check_rooms(d.get("rooms", []))
        dialout = bool(d.get("dialout", s.get("dialout", True)))
        codec = str(d.get("codec", s.get("codec", "3"))).strip()
        if not re.fullmatch(r"\d{1,5}", codec):
            raise JitsiError("Codec-Liste: Nummer der voice class codec.")
        if not host_ip:
            raise JitsiError("VM_HOST_IP fehlt – ohne Adresse von Kamailio keine Dial-Peers.")
        s.update(rooms=rooms, dialout=dialout, codec=codec)
        if s["host"] and not s.get("jigasi_ip"):
            s["jigasi_ip"] = resolve(s["host"])
        save(s)
        write_conf(s)
        lines = cme_lines(rooms, router.show("show running-config | section dial-peer voice"), host_ip, conf_port,
                          s.get("jigasi_ip", ""), dialout, codec)
        res = apply(lines) if lines else {"ok": True, "config": []}
        return jsonify(ok=res["ok"], error=res.get("error"), config=lines)

    @app.post("/api/jitsi/prosody-allow")
    def jitsi_prosody_allow():
        with lock:
            props = parse_props(read_props())
            m = model(props, host_ip, conf_port)
            jid = props.get(XMPP_ACC + "USER_ID", "").lower()
            if not m["domain"] or not jid:
                raise JitsiError("Domain von Jitsi oder Jigasi-Konto (acc-xmpp-1.USER_ID) nicht erkannt.")
            out = ssh("sudo -n sh -s", stdin=prosody_allow_script(m["domain"], jid), timeout=90)
            out += ssh("sudo -n sh -s", stdin=jicofo_trust_script(jid.split("@", 1)[1]), timeout=60)
        return jsonify(ok=True, changed="=neu" in out)

    @app.post("/api/jitsi/prosody-cert")
    def jitsi_prosody_cert():
        with lock:
            m = model(parse_props(read_props()), host_ip, conf_port)
            dom = m["domain"]
            if not dom:
                raise JitsiError("Domain von Jitsi nicht erkannt.")
            le = f"/etc/letsencrypt/live/{dom}"
            hook = prosody_hook(le, [dom, f"auth.{dom}"])
            # Hook ablegen und gleich einmal ausführen (Prosody, Jicofo, Videobridge und Jigasi starten neu)
            ssh(f"sudo -n test -f {le}/fullchain.pem || {{ echo 'Kein Zertifikat in {le}'; exit 1; }}; "
                "sudo -n sh -c 'H=/etc/letsencrypt/renewal-hooks/deploy/prosody-cme-web.sh; mkdir -p \"$(dirname $H)\" && "
                "cat > \"$H\" && chmod 755 \"$H\" && \"$H\"'", stdin=hook, timeout=90)
        return jsonify(ok=True)

