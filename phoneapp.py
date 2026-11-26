"""
phoneapp.py - CME Phone (eigenes Softphone, Ordner softphone/) in cme_web

Verwaltung (Tab "Softphone", nur Administratoren):
  * APNs-Schluessel (.p8) hochladen, Key-ID/Team-ID/Umgebung - liegt im gemeinsamen Volume mit dem
    push-relay (PHONEAPP_DATA_DIR, im Relay RELAY_DATA_DIR); der Relay liest Aenderungen sofort
  * Secret fuer den Local-Push-Kanal (Heim-WLAN) und App-Schluessel fuer die Client-Schnittstelle
  * Status des Relays (status.json): verbundene Geraete, letzter Push, APNs-Ergebnis
  * Werte fuer die Einrichtung der Apps (Gateway, Ports)

Client-Schnittstelle fuer die Apps (/client/v1/..., Anmeldung mit "Authorization: Bearer <App-Schluessel>"):
  GET  /client/v1/info?ext=21            Einstellungen fuer die App (Gateway, CME, Local Push, Voicemail-Nummer,
                                         Aufzeichnung verfuegbar?) - die App braucht nur URL, Schluessel und Nebenstelle
  GET  /client/v1/calls?ext=21           laufende Gespraeche der Nebenstelle beim cme-recorder (extern, SIPREC)
  POST /client/v1/calls/<id>             {"ext": "21", "on": true|false}  Aufnahme ein/aus
  GET  /client/v1/voicemail?ext=21       Nachrichten im Postfach der Nebenstelle; .../<box>/<folder>/<file>/audio (WAV),
                                         POST {"ext", "heard"} gehört/neu, DELETE löschen
  GET  /client/v1/recordings?ext=21      Gesprächsaufnahmen der Nebenstelle; .../<id>/audio (MP3/WAV), DELETE löschen
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import Response, jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_KEYID = re.compile(r"^[A-Z0-9]{10}$")
RE_EXT = re.compile(r"^\+?[0-9]{1,32}$")
RE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
RE_REC_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
# Passwörter/Namen in der Flexisip-Passwortdatei (Grammatik "token" ohne Leerzeichen und ";")
RE_PW_TOKEN = re.compile(r"^[A-Za-z0-9._!%*+~-]{1,64}$")
ENVS = ("auto", "sandbox", "production")
DEVICE_PREFIX = "CSF"          # Geraetename der App am Router (wie cme_web.SOFTPHONE_PREFIX)
# Mobilgeraete (iPhone/iPad) einer Rufnummer: EIN Pool "MOB<Nummer>", den cme-web (sharedline.py) mit Ziel
# Flexisip an CME anmeldet. Die Apps melden sich nur an Flexisip an; das Passwort dieses Pools prueft cme-web.
MOBILE_PREFIX = "MOB"
# Codec-Liste 3 (G.722, G.711); ohne Angabe bietet CME G.729 an, das liblinphone nicht hat
MOBILE_CODEC = " voice-class codec 3"


class AppError(ValueError):
    pass


def validate_p8(pem: bytes) -> str:
    """Prueft, dass es ein EC-P-256-Schluessel im PEM-Format ist (wie Apple ihn als .p8 liefert)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    try:
        text = pem.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise AppError("Keine .p8-Datei (keine Textdatei).") from None
    if "BEGIN PRIVATE KEY" not in text:
        raise AppError("Keine .p8-Datei („BEGIN PRIVATE KEY“ fehlt).")
    try:
        key = load_pem_private_key(text.encode(), password=None)
    except Exception as e:  # noqa: BLE001
        raise AppError(f"Schlüssel nicht lesbar: {e}") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise AppError("Kein APNs-Schlüssel (erwartet EC P-256).")
    return text + "\n"


def key_id_from_name(name: str) -> str:
    """AuthKey_ABCDE12345.p8 -> ABCDE12345"""
    m = re.search(r"([A-Z0-9]{10})\.P8$", (name or "").upper())
    return m.group(1) if m else ""


def harmless(line: str) -> bool:
    """"create profile" meldet "%Error deleting flash:/its/<Datei>", wenn es eine Datei (jabber-config.xml,
    <Geraet>.cnf.xml) noch nicht gibt - harmlos."""
    return "Error deleting flash:" in line or "jabber-config" in line


def tail(n: str) -> str:
    d = re.sub(r"\D", "", n or "")
    return d[-8:] if len(d) >= 8 else d


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    tmp.replace(path)


def register(app, router, demo: bool, cfgdir: Path) -> None:  # noqa: ARG001
    data = Path(os.environ.get("PHONEAPP_DATA_DIR", "/data/push"))
    if demo:
        import tempfile
        data = Path(tempfile.gettempdir()) / "cme-web-demo-push"
    store = cfgdir / "cme-phoneapp.json"
    # SIP-Zugangsdaten der CME-Phone-Pools: der Router speichert Passwoerter verschluesselt (password 6/7),
    # die App braucht sie aber im Klartext -> cme-web merkt sie sich, wenn es sie setzt.
    creds_path = cfgdir / "cme-phoneapp-creds.json"
    apns_dir = data / "apns"
    key_path, meta_path = apns_dir / "AuthKey.p8", apns_dir / "apns.json"
    secret_path = data / "localpush-secret"
    lock = threading.Lock()
    team_default = os.environ.get("APNS_TEAM_ID", "").strip().upper()
    bundle_id = os.environ.get("APNS_BUNDLE_ID", "de.wiederhol.cmephone")
    host_ip = os.environ.get("VM_HOST_IP", "").strip()

    # ---------------- Einstellungen ----------------

    def settings() -> dict:
        s = {"token": "", "gateway_host": os.environ.get("SIP_GW_NAME", "").strip() or host_ip,
             "gateway_port": int(os.environ.get("SIP_GW_PORT", "5062") or 5062),
             "localpush_port": int(os.environ.get("RELAY_LOCALPUSH_PORT", "5072") or 5072),
             "ssids": ""}
        try:
            s.update(json.loads(store.read_text()))
        except (FileNotFoundError, ValueError):
            pass
        return s

    def save(s: dict) -> None:
        _write(store, json.dumps(s, indent=1), 0o600)

    def exec_pw() -> str:
        """Passwort fuer die Telefon-Steuerung (CGI/Execute): nur Buchstaben/Ziffern, wie CME es verlangt."""
        import string
        while True:
            pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(15))
            if any(c.isupper() for c in pw) and any(c.islower() for c in pw) and any(c.isdigit() for c in pw):
                return pw

    def ensure() -> dict:
        """App-Schluessel, Steuerungs-Zugang und Local-Push-Secret beim ersten Mal erzeugen."""
        with lock:
            s = settings()
            changed = False
            if not s.get("token"):
                s["token"] = secrets.token_urlsafe(24)
                changed = True
            if not s.get("exec_password"):
                s["exec_user"] = s.get("exec_user") or "cmephone"
                s["exec_password"] = exec_pw()
                changed = True
            if changed:
                save(s)
            try:
                if not secret_path.exists():
                    _write(secret_path, (os.environ.get("RELAY_LOCALPUSH_SECRET", "").strip()
                                         or secrets.token_hex(24)) + "\n", 0o600)
            except OSError:
                pass
            return s

    def creds_all() -> dict:
        try:
            return json.loads(creds_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def creds_store(device: str, username: str, password: str) -> None:
        with lock:
            c = creds_all()
            c[device.upper()] = {"username": username, "password": password, "set": time.strftime("%Y-%m-%d %H:%M")}
            _write(creds_path, json.dumps(c, indent=1), 0o600)

    # fuer cme_web.py: Telefon anlegen/bearbeiten mit Passwort -> hier merken
    app.config["PHONEAPP_CREDS"] = creds_store

    # Mobilgeraete (iPhone, iPad, Vision Pro): melden sich nur an Flexisip an, jedes mit eigenem Namen
    # (z.B. MOBIphone) und Passwort, das cme-web prueft. Fuer CME bilden alle Mobilgeraete einer Nummer
    # EINEN Pool MOB<Nummer> (Ziel Flexisip), den sharedline.py anmeldet.
    mobile_path = cfgdir / "cme-phoneapp-mobile.json"

    def mobiles() -> dict:
        try:
            return json.loads(mobile_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def mobile_save(m: dict) -> None:
        _write(mobile_path, json.dumps(m, indent=1), 0o600)
        write_flexisip_passwd(m)

    # Passwortdatei für Flexisip (module::Authentication, db-implementation=file): Flexisip prüft damit die
    # Anfragen der Apps (INVITE, re-INVITE ...) per Digest. Gesucht wird nach User und Host des From und dem
    # Anmeldenamen der App; der From-Host ist je nach Gespräch die SIP-Domain (abgehend), die CME-IP
    # (Übernehmen) oder die IP von Flexisip (eingehend, To der CME). Volume softphone-data, in Flexisip nur lesbar.
    flexisip_passwd = data / "flexisip-passwd"

    def write_flexisip_passwd(m: dict | None = None) -> None:
        m = mobiles() if m is None else m
        # Apps-Domain des B2BUA (flexisip/entrypoint.sh: B2BUA_APP_DOMAIN, Standard SIP_GW_NAME): bei Anrufen an die
        # Apps steht sie im From der re-INVITEs (Halten, Video) der Apps
        app_domain = (os.environ.get("B2BUA_APP_DOMAIN", "").strip() or os.environ.get("SIP_GW_NAME", "").strip()
                      or "apps.invalid")
        domains = [d for d in dict.fromkeys([os.environ.get("SIP_DOMAIN", "").strip(), os.environ.get("CME_HOST", "").strip(),
                                             os.environ.get("VM_HOST_IP", "").strip(), app_domain]) if d]
        # Realm des Digest: immer die SIP-Domain (flexisip.conf.template: realm=${SIP_DOMAIN})
        realm = os.environ.get("SIP_DOMAIN", "").strip() or "cme-web"
        # ohne Kommentarzeile: Flexisip bricht schon an "(" in einem Kommentar ab (Grammatik authdb-file)
        lines = ["version:1"]
        for name, v in sorted(m.items()):
            num, pw = v.get("number", ""), v.get("password", "")
            if num and RE_PW_TOKEN.match(pw) and RE_PW_TOKEN.match(name):
                # Fertiger HA1 statt clrtxt: aus clrtxt rechnet Flexisip den Hash mit der Rufnummer (User des From),
                # die App aber mit ihrem Anmeldenamen -> "Passwords did not match", 403
                ha1 = hashlib.md5(f"{name}:{realm}:{pw}".encode()).hexdigest()
                lines += [f"{num}@{d} md5:{ha1} ; {name}" for d in domains]
        try:
            _write(flexisip_passwd, "\n".join(lines) + "\n", 0o600)
        except OSError as e:
            print(f"Softphone: {flexisip_passwd} nicht beschreibbar ({e})", flush=True)

    def mobile_account(name: str) -> dict:
        """{"number", "password"} des Mobilgeraets (Gross-/Kleinschreibung egal) oder {}"""
        return next((v for k, v in mobiles().items() if k.lower() == (name or "").lower()), {})

    def app_pools() -> list[dict]:
        """Pools mit Geraetename CSF... aus der Router-Konfiguration."""
        try:
            out = router.show("show running-config | section voice register pool")
        except Exception:  # noqa: BLE001
            return []
        pools, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^voice register pool\s+(\d+)", line)
            if m:
                cur = {"tag": m.group(1), "device": "", "name": "", "username": "", "plain": "", "dns": [], "type": ""}
                pools.append(cur)
                continue
            if cur is None:
                continue
            parts = line.split()
            if parts[:2] == ["id", "device-id-name"] and len(parts) == 3:
                cur["device"] = parts[2].upper()
                cur["name"] = parts[2]          # Schreibweise wie am Router (z.B. SIPMacMini)
            elif parts[:1] == ["username"] and len(parts) >= 4 and parts[2] == "password":
                cur["username"] = parts[1]
                cur["plain"] = parts[3] if len(parts) == 4 else ""
            elif parts[:1] == ["number"] and len(parts) >= 4:
                cur["dns"].append(parts[3])
            elif parts[:1] == ["type"] and len(parts) >= 2:
                cur["type"] = parts[1]
        # CME Phone: Geraetename frei waehlbar (z.B. SIPMacMini), Typ Jabber-CSF-Client; aeltere Pools: CSF…/MOB…
        return [p for p in pools if p["device"] and (p["type"].lower() == "jabber-csf-client"
                                                      or p["device"].startswith((DEVICE_PREFIX, MOBILE_PREFIX)))]

    def new_pw() -> str:
        import string
        abc = string.ascii_letters + string.digits
        while True:
            pw = "".join(secrets.choice(abc) for _ in range(12))
            if re.search(r"\d", pw) and re.search(r"[A-Z]", pw) and re.search(r"[a-z]", pw):
                return pw

    def lp_secret() -> str:
        try:
            return secret_path.read_text().strip()
        except OSError:
            return os.environ.get("RELAY_LOCALPUSH_SECRET", "").strip()

    def relay_status() -> dict:
        try:
            st = json.loads((data / "status.json").read_text())
        except (OSError, ValueError):
            return {"online": False}
        st["online"] = time.time() - st.get("time", 0) < 30
        return st

    def apns_meta() -> dict:
        try:
            m = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            m = {}
        m["configured"] = key_path.exists() and bool(m.get("key_id"))
        return m

    def cme_url() -> str:
        return os.environ.get("CME_PUBLIC_URL", "").strip().rstrip("/") or request.host_url.rstrip("/")

    def auth_url() -> str:
        """Authentication URL fuer die Tischtelefone (Port, unter dem die Telefone cme-web erreichen)."""
        port = int(os.environ.get("CME_PHONE_URL_PORT") or os.environ.get("CME_PHONE_PORT") or 8081)
        return f"http://{host_ip or '<docker-host>'}:{port}/ipphone/authenticate"

    def pilot() -> str:
        vm = app.config.get("VOICEMAIL") or {}
        try:
            on = vm.get("enabled")
            on = on() if callable(on) else on
            return str(vm["settings"]().get("pilot", "")) if vm and on else ""
        except Exception:  # noqa: BLE001
            return ""

    def rec() -> dict | None:
        r = app.config.get("RECORDING") or {}
        try:
            return r if r and r["enabled"]() and "active" in r else None
        except Exception:  # noqa: BLE001
            return None

    # ---------------- Verwaltung ----------------

    @app.errorhandler(AppError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/phoneapp.js")
    def phoneapp_js():
        return send_file(BASE / "static" / "phoneapp.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/phoneapp")
    def admin_status():
        s = ensure()
        m = apns_meta()
        return jsonify(ok=True, relay=relay_status(), apns={
            "configured": m["configured"], "key_id": m.get("key_id", ""), "team_id": m.get("team_id", "") or team_default,
            "env": m.get("env", "auto"), "uploaded": m.get("uploaded", "")},
            token=s["token"], secret=lp_secret(), url=cme_url(), bundle_id=bundle_id,
            gateway_host=s["gateway_host"], gateway_port=s["gateway_port"], localpush_port=s["localpush_port"],
            ssids=s.get("ssids", ""), cme_host=os.environ.get("CME_HOST", ""), prefix=DEVICE_PREFIX,
            exec_user=s.get("exec_user", "cmephone"), exec_password=s.get("exec_password", ""), auth_url=auth_url())

    @app.post("/api/phoneapp/apns")
    def admin_apns():
        """multipart: keyfile (optional, wenn schon einer da ist), key_id, team_id, env"""
        f = request.files.get("keyfile")
        key_id = (request.form.get("key_id") or "").strip().upper()
        team_id = (request.form.get("team_id") or "").strip().upper()
        env = (request.form.get("env") or "auto").strip()
        if f and f.filename and not key_id:
            key_id = key_id_from_name(f.filename)
        if not key_id:
            key_id = apns_meta().get("key_id", "")
        if not RE_KEYID.match(key_id):
            raise AppError("Key-ID muss aus 10 Zeichen A–Z/0–9 bestehen (steht im Dateinamen AuthKey_<Key-ID>.p8).")
        if not RE_KEYID.match(team_id):
            raise AppError("Team-ID muss aus 10 Zeichen A–Z/0–9 bestehen (Apple Developer → Membership).")
        if env not in ENVS:
            raise AppError("Umgebung muss auto, sandbox oder production sein.")
        with lock:
            if f and f.filename:
                raw = f.read(16384)
                _write(key_path, validate_p8(raw), 0o600)
            elif not key_path.exists():
                raise AppError("Bitte die .p8-Datei auswählen.")
            _write(meta_path, json.dumps({"key_id": key_id, "team_id": team_id, "env": env,
                                          "uploaded": time.strftime("%Y-%m-%d %H:%M:%S")}), 0o600)
        return jsonify(ok=True)

    @app.delete("/api/phoneapp/apns")
    def admin_apns_delete():
        with lock:
            for p in (key_path, meta_path):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
        return jsonify(ok=True)

    @app.post("/api/phoneapp/settings")
    def admin_settings():
        d = request.get_json(force=True) or {}
        host = str(d.get("gateway_host", "")).strip()
        if host and not RE_HOST.match(host):
            raise AppError("Ungültiger Name/IP für das Push-Gateway.")
        try:
            port = int(d.get("gateway_port") or 5062)
            lport = int(d.get("localpush_port") or 5072)
        except (TypeError, ValueError):
            raise AppError("Ungültiger Port.") from None
        if not (1 <= port <= 65535 and 1 <= lport <= 65535):
            raise AppError("Ungültiger Port.")
        ssids = ",".join(x.strip() for x in str(d.get("ssids", "")).split(",") if x.strip())[:500]
        with lock:
            s = settings()
            s.update(gateway_host=host, gateway_port=port, localpush_port=lport, ssids=ssids)
            save(s)
        return jsonify(ok=True)

    @app.get("/api/phoneapp/devices")
    def admin_devices():
        saved = creds_all()
        out = []
        # Anmeldung an CME (Mac direkt, MOB-Pool über cme-web): show voice register pool all brief
        try:
            from cme_web import parse_pool_brief
            brief = parse_pool_brief(router.show("show voice register pool all brief"))
        except Exception:  # noqa: BLE001
            brief = {}
        for p in app_pools():
            sv = saved.get(p["device"])
            ok = bool(sv and (not p["username"] or sv["username"] == p["username"])) or bool(p["plain"])
            b = brief.get(p["tag"]) or {}
            out.append({"tag": p["tag"], "device": p["name"] or p["device"], "username": p["username"], "dns": p["dns"],
                        "creds": ok, "set": (sv or {}).get("set", ""), "state": b.get("state", ""), "ip": b.get("ip", ""),
                        "kind": "mobile" if p["device"].startswith(MOBILE_PREFIX) else "app"})
        # Rufnummern fuer "Mobilgeraete einrichten" (Pool MOB<Nummer>)
        numbers = []
        try:
            from cme_web import parse_voice_register_config
            dns, _ = parse_voice_register_config(router.show("show running-config | section voice register"))
            have = {p["device"].upper() for p in out}
            numbers = [{"tag": t, "number": d["number"], "shared": d["shared"],
                        "mobile": (MOBILE_PREFIX + d["number"].lstrip("+")).upper() in have}
                       for t, d in sorted(dns.items(), key=lambda x: (not x[1].get("shared"), int(x[0]))) if d.get("number")]
        except Exception:  # noqa: BLE001
            pass
        return jsonify(ok=True, devices=out, numbers=numbers)

    @app.post("/api/phoneapp/devices/<tag>/credentials")
    def admin_set_credentials(tag):
        """Neues SIP-Passwort fuer einen CME-Phone-Pool setzen und fuer die Apps merken."""
        pool = next((p for p in app_pools() if p["tag"] == tag), None)
        if not pool:
            raise AppError(f"Pool {tag} ist kein CME-Phone-Pool.")
        username = pool["username"] or pool["device"]
        pw = new_pw()
        out = router.configure([f"voice register pool {tag}", f" username {username} password {pw}",
                                "voice register global", " create profile"])
        # "create profile" meldet "%Error deleting flash:/its/jabber-config.xml", wenn es die Datei noch
        # nicht gibt - harmlos
        bad = [l for l in str(out).splitlines() if not harmless(l)
               and (l.strip().startswith("%") or "is in use" in l or "Invalid" in l or "Error" in l)]
        if bad:
            raise AppError("Der Router hat das Passwort nicht übernommen:\n" + "\n".join(bad[:5]))
        try:
            router.save()
        except Exception:  # noqa: BLE001
            pass
        creds_store(pool["device"], username, pw)
        return jsonify(ok=True, username=username)

    def configure_ok(lines: list[str], what: str) -> None:
        out = router.configure(lines)
        bad = [l for l in str(out).splitlines() if not harmless(l)
               and (l.strip().startswith("%") or "is in use" in l or "Invalid" in l or "Error" in l)]
        if bad:
            raise AppError(f"Der Router hat {what} nicht übernommen:\n" + "\n".join(bad[:5]))
        try:
            router.save()
        except Exception:  # noqa: BLE001
            pass

    def ensure_group_pool(tag_dn: str) -> str:
        """Pool MOB<Nummer> fuer die Mobilgeraete dieser Rufnummer anlegen (oder fehlende Zugangsdaten setzen)."""
        from cme_web import parse_voice_register_config
        dns, pools = parse_voice_register_config(router.show("show running-config | section voice register"))
        dn = dns.get(tag_dn)
        if not dn or not dn.get("number"):
            raise AppError("Unbekannte Rufnummer.")
        device = (MOBILE_PREFIX + dn["number"].lstrip("+")).upper()
        have = next((t for t, p in pools.items() if p["device_id"].upper() == device), None)
        if have:
            fix = []
            if not pools[have].get("codec") and not pools[have].get("codec_list"):
                # Ohne Codec-Angabe bietet CME G.729 an - das kann die App nicht (488 Not acceptable here)
                fix.append(MOBILE_CODEC)
            pw = None
            if not device_credentials(device):
                pw = new_pw()
                fix.append(f" username {device} password {pw}")
            if fix:
                configure_ok([f"voice register pool {have}", *fix, "voice register global", " create profile"],
                             "den Pool")
            if pw:
                creds_store(device, device, pw)
            return device
        tag = str(max([int(t) for t in pools] + [0]) + 1)
        pw = new_pw()
        lines = []
        if not dn.get("shared"):
            lines += [f"voice register dn {tag_dn}", " shared-line max-calls 5"]
        lines += [f"voice register pool {tag}", f" id device-id-name {device}", " type Jabber-CSF-Client",
                  f" number 1 dn {tag_dn}", f" username {device} password {pw}", " dtmf-relay rtp-nte sip-notify", MOBILE_CODEC,
                  " description CME Mobil", "voice register global", " create profile"]
        configure_ok(lines, "den Pool")
        creds_store(device, device, pw)
        return device

    RE_MOBNAME = re.compile(r"^[A-Za-z0-9]{3,20}$")

    @app.get("/api/phoneapp/mobiles")
    def admin_mobiles():
        return jsonify(ok=True, devices=[{"name": k, "number": v["number"], "set": v.get("set", ""),
                                          "push": bool(v.get("apns", {}).get("token"))}
                                         for k, v in sorted(mobiles().items())])

    @app.post("/api/phoneapp/mobiles")
    def admin_mobile_add():
        """{"name": "MOBIphone", "dn": "<tag>"} - Mobilgeraet anlegen (Passwort erzeugt cme-web)."""
        from cme_web import parse_voice_register_config
        d = request.get_json(force=True) or {}
        name = str(d.get("name", "")).strip()
        if not RE_MOBNAME.match(name):
            raise AppError("Name: 3–20 Buchstaben/Ziffern, z. B. MOBIphone.")
        if mobile_account(name) or any(p["device"].lower() == name.lower() for p in app_pools()):
            raise AppError(f"Den Namen {name} gibt es schon.")
        tag_dn = str(d.get("dn", "")).strip()
        dns, _ = parse_voice_register_config(router.show("show running-config | section voice register"))
        if tag_dn not in dns or not dns[tag_dn].get("number"):
            raise AppError("Unbekannte Rufnummer.")
        group = ensure_group_pool(tag_dn)
        with lock:
            m = mobiles()
            m[name] = {"number": dns[tag_dn]["number"], "password": new_pw(), "set": time.strftime("%Y-%m-%d %H:%M")}
            mobile_save(m)
        return jsonify(ok=True, name=name, pool=group)

    @app.delete("/api/phoneapp/mobiles/<name>")
    def admin_mobile_delete(name):
        with lock:
            m = mobiles()
            key = next((k for k in m if k.lower() == name.lower()), None)
            if key is None:
                raise AppError("Unbekanntes Gerät.")
            del m[key]
            mobile_save(m)
        return jsonify(ok=True)

    @app.post("/api/phoneapp/mobiles/<name>/password")
    def admin_mobile_password(name):
        with lock:
            m = mobiles()
            key = next((k for k in m if k.lower() == name.lower()), None)
            if key is None:
                raise AppError("Unbekanntes Gerät.")
            m[key]["password"] = new_pw()
            m[key]["set"] = time.strftime("%Y-%m-%d %H:%M")
            mobile_save(m)
        return jsonify(ok=True)

    @app.post("/api/phoneapp/renew")
    def admin_renew():
        """{"what": "token" | "secret"} - neuer App-Schluessel bzw. neues Local-Push-Secret"""
        what = (request.get_json(force=True) or {}).get("what")
        with lock:
            if what == "token":
                s = settings()
                s["token"] = secrets.token_urlsafe(24)
                save(s)
            elif what == "secret":
                _write(secret_path, secrets.token_hex(24) + "\n", 0o600)
            elif what == "exec":
                s = settings()
                s["exec_password"] = exec_pw()
                save(s)
            else:
                raise AppError("Unbekannt.")
        return jsonify(ok=True)

    # ---------------- Authentication URL der Tischtelefone ----------------
    # Ein Cisco-Telefon prueft bei jedem CGI/Execute die Basic-Auth-Daten, indem es diese URL mit
    # UserID/Password/devicename aufruft. CMEs eigenes authenticate.asp kennt nur SCCP-Telefone
    # (liefert fuer SIP-Telefone UN_AUTHORIZED) - deshalb beantwortet cme-web die Abfrage selbst.

    @app.get("/ipphone/authenticate")
    @app.get("/ipphone/authenticate.asp")
    def phone_authenticate():
        s = ensure()
        user = request.args.get("UserID", "")
        pw = request.args.get("Password", "")
        want_u, want_p = s.get("exec_user", "cmephone"), s.get("exec_password", "")
        ok = bool(want_p) and secrets.compare_digest(user.encode(), want_u.encode()) \
            and secrets.compare_digest(pw.encode(), want_p.encode())
        if not ok:
            time.sleep(0.5)
        return Response("AUTHORIZED" if ok else "UN-AUTHORIZED", mimetype="text/plain")

    # ---------------- Client-Schnittstelle fuer die Apps ----------------

    def client_auth() -> None:
        tok = settings().get("token", "")
        got = request.headers.get("Authorization", "")
        got = got[7:].strip() if got.lower().startswith("bearer ") else ""
        if not tok or not got or not secrets.compare_digest(got.encode(), tok.encode()):
            raise PermissionError

    def ext_arg(d: dict | None = None) -> str:
        ext = str((d or {}).get("ext") or request.args.get("ext") or "").strip()
        if not RE_EXT.match(ext):
            raise AppError("Nebenstelle fehlt oder ist ungültig.")
        return ext

    def my_calls(ext: str) -> list[dict]:
        r = rec()
        if not r:
            return []
        mine = tail(ext)
        out = []
        for a in r["active"]():
            nums = a.get("numbers", [])
            if mine in {tail(n) for n in nums}:
                other = next((n for n in nums if tail(n) != mine), "?")
                out.append({"id": a["id"], "other": other, "recording": bool(a.get("recording")),
                            "start": a.get("start", 0)})
        return sorted(out, key=lambda a: -a["start"])

    @app.before_request
    def client_guard():
        if not request.path.startswith("/client/"):
            return None
        try:
            client_auth()
        except PermissionError:
            return jsonify(ok=False, error="App-Schlüssel fehlt oder ist falsch."), 401
        return None

    def pool_device(ext: str, prefix: str = DEVICE_PREFIX, name: str = "") -> str:
        """Geraetename des Pools fuer diese Nummer: der Name aus der App (z.B. SIPMacMini), sonst CSF<ext> bzw.
        MOB<ext> - oder der CME-Phone-Pool, an dem die Nummer haengt (Name passt nicht mehr zur Nummer)."""
        allp = app_pools()
        if name and any(p["device"] == name.upper() for p in allp):
            return name.upper()
        want = (prefix + ext.lstrip("+")).upper()
        pools = [p for p in allp if p["device"].startswith(prefix)
                 or (prefix == DEVICE_PREFIX and not p["device"].startswith(MOBILE_PREFIX))]
        if any(p["device"] == want for p in pools):
            return want
        try:
            from cme_web import parse_voice_register_config
            dns, _ = parse_voice_register_config(router.show("show running-config | section voice register"))
        except Exception:  # noqa: BLE001
            return want
        tags = {t for t, d in dns.items() if tail(d.get("number", "")) == tail(ext)}
        hit = next((p["device"] for p in pools if tags & set(p["dns"])), "")
        return hit or want

    def pool_credentials(ext: str, prefix: str = DEVICE_PREFIX) -> dict:
        """SIP-Zugangsdaten des Pools CSF<ext> (bzw. MOB<ext>): aus cme-web (hier gesetzt) oder Klartext aus dem
        Router. Passt der gespeicherte Benutzer nicht mehr zum Pool, gilt er als veraltet."""
        return device_credentials(pool_device(ext, prefix))

    def device_credentials(want: str, pools: list[dict] | None = None) -> dict:
        pool = next((p for p in (pools if pools is not None else app_pools()) if p["device"] == want), None)
        saved = creds_all().get(want)
        if saved and (pool is None or not pool["username"] or pool["username"] == saved["username"]):
            return {"username": saved["username"], "password": saved["password"]}
        if pool and pool["username"] and pool["plain"]:
            return {"username": pool["username"], "password": pool["plain"]}
        return {}

    @app.get("/client/v1/info")
    def client_info():
        ext = ext_arg()
        s = ensure()
        name = str(request.args.get("device", "")).strip()[:40]
        mob = mobile_account(name) if name else {}
        if mob and tail(mob["number"]) != tail(ext):
            raise AppError(f"Das Gerät {name} gehört zu {mob['number']}, nicht zu {ext}.")
        r = rec()
        # SIP-Domain der Apps: ein Name (Flexisip nimmt Anmeldungen mit fremder IP-Domain nicht an),
        # Flexisip leitet ihn per Route an CME_HOST weiter
        device = pool_device(ext, DEVICE_PREFIX, "" if mob else name)
        device_raw = next((p["name"] for p in app_pools() if p["device"] == device and p["name"]), device)
        return jsonify(ok=True, ext=ext, device_name=name if mob else device_raw,
                       cme_host=os.environ.get("SIP_DOMAIN", "").strip() or os.environ.get("CME_HOST", ""),
                       # direkte Anmeldung an CME (Mac, wie Jabber): Shared Line funktioniert nur ohne Flexisip
                       cme_ip=os.environ.get("CME_HOST", "").strip(),
                       gateway={"host": s["gateway_host"], "port": s["gateway_port"], "transport": "tcp"},
                       localpush={"host": s["gateway_host"], "port": s["localpush_port"], "secret": lp_secret(),
                                  "ssids": [x for x in s.get("ssids", "").split(",") if x]},
                       voicemail=pilot(),
                       auth=device_credentials(device),
                       # iPhone/iPad/Vision Pro: Anmeldung nur an Flexisip mit eigenem Namen und Passwort (leer = Gerät
                       # in cme-web nicht angelegt)
                       mobile_auth=({"username": next(k for k in mobiles() if k.lower() == name.lower()),
                                     "password": mob["password"]} if mob else {}),
                       deskphone={"user": s.get("exec_user", "cmephone"), "password": s.get("exec_password", "")},
                       recording={"available": bool(r), "mode": (r["settings"]().get("mode", "always") if r else "")})

    @app.get("/client/v1/calls")
    def client_calls():
        return jsonify(ok=True, calls=my_calls(ext_arg()))

    @app.post("/client/v1/calls/<rid>")
    def client_call_set(rid):
        d = request.get_json(force=True) or {}
        ext = ext_arg(d)
        if not RE_REC_ID.match(rid):
            raise AppError("Ungültiges Gespräch.")
        if rid not in {a["id"] for a in my_calls(ext)}:
            raise AppError("Kein laufendes Gespräch dieser Nebenstelle.")
        on = bool(d.get("on"))
        rec()["set"](rid, on)
        return jsonify(ok=True, on=on)

    # ---------------- Voicemail und Gesprächsaufnahmen in den Apps ----------------
    # Wie Visual Voicemail am 8845 (vvm.py) und die Benutzerseite (portal.py), aber mit dem App-Schlüssel: nur
    # das Postfach bzw. die Aufnahmen der Nebenstelle der App.

    RE_VM_FOLDER = re.compile(r"^(INBOX|Old)$")
    RE_VM_FILE = re.compile(r"^msg\d{4}$")

    def vm() -> dict:
        v = app.config.get("VOICEMAIL") or {}
        return v if v.get("enabled") and "msg_path" in v else {}    # enabled ist hier ein Wert (voicemail.py)

    def my_box(ext: str) -> str:
        """Postfach (mbid) der Nebenstelle oder "" """
        v = vm()
        if not v:
            return ""
        box = next((n for n in v["settings"]()["mailboxes"] if tail(n) == tail(ext)), None)
        return v["mbid"](box) if box else ""

    def vm_check(ext: str, box: str, folder: str, file: str) -> dict:
        if not (box and box == my_box(ext) and RE_VM_FOLDER.match(folder) and RE_VM_FILE.match(file)):
            raise AppError("Nachricht nicht gefunden.")
        return vm()

    @app.get("/client/v1/voicemail")
    def client_voicemail():
        ext = ext_arg()
        box = my_box(ext)
        msgs = vm()["messages"](box) if box else []
        return jsonify(ok=True, box=box, messages=[{**m, "box": box, "new": m["folder"] == "INBOX"} for m in msgs])

    @app.get("/client/v1/voicemail/<box>/<folder>/<file>/audio")
    def client_voicemail_audio(box, folder, file):
        p = vm_check(ext_arg(), box, folder, file)["msg_path"](box, folder, file).with_suffix(".wav")
        if not p.exists():
            return jsonify(ok=False, error="Aufnahme nicht gefunden"), 404
        return send_file(p, mimetype="audio/wav", max_age=0, conditional=True)

    @app.post("/client/v1/voicemail/<box>/<folder>/<file>")
    def client_voicemail_set(box, folder, file):
        """{"ext": …, "heard": true|false} – gehört (Old) bzw. neu (INBOX)"""
        d = request.get_json(force=True) or {}
        v = vm_check(ext_arg(d), box, folder, file)
        to = "Old" if d.get("heard") else "INBOX"
        if to != folder:
            v["msg_action"](box, folder, file, {"Action": "VoicemailMove", "ToFolder": to})
        return jsonify(ok=True)

    @app.delete("/client/v1/voicemail/<box>/<folder>/<file>")
    def client_voicemail_delete(box, folder, file):
        vm_check(ext_arg(), box, folder, file)["msg_action"](box, folder, file, {"Action": "VoicemailRemove"})
        return jsonify(ok=True)

    def rec_files() -> dict:
        r = app.config.get("RECORDING") or {}
        try:
            return r if r and r["enabled"]() and "audio" in r else {}
        except Exception:  # noqa: BLE001
            return {}

    def rec_check(ext: str, rid: str) -> dict:
        r = rec_files()
        if not (r and RE_REC_ID.match(rid) and r["may"](rid, [ext])):
            raise AppError("Aufnahme nicht gefunden.")
        return r

    @app.get("/client/v1/recordings")
    def client_recordings():
        ext = ext_arg()
        r = rec_files()
        return jsonify(ok=True, enabled=bool(r), recordings=r["list"]([ext]) if r else [])

    @app.get("/client/v1/recordings/<rid>/audio")
    def client_recording_audio(rid):
        p = rec_check(ext_arg(), rid)["audio"](rid)
        if not p:
            return jsonify(ok=False, error="Aufnahme nicht gefunden"), 404
        return send_file(p, mimetype="audio/mpeg" if p.suffix == ".mp3" else "audio/wav", max_age=0, conditional=True)

    @app.delete("/client/v1/recordings/<rid>")
    def client_recording_delete(rid):
        rec_check(ext_arg(), rid)["delete"](rid)
        return jsonify(ok=True)

    # ---------------- Mitteilungen an die Mobilgeräte (gehaltene Gespräche) ----------------

    RE_APNS_TOKEN = re.compile(r"^[0-9a-fA-F]{32,200}$")
    outbox = data / "outbox"

    @app.post("/client/v1/push")
    def client_push():
        """{"device": "MOBIphone", "token": "<APNs-Token hex>", "env": "prod|sandbox"} – normaler Push-Token der App
        (Mitteilungen; der VoIP-Token für Anrufe steckt in der Anmeldung bei Flexisip)."""
        d = request.get_json(force=True) or {}
        name = str(d.get("device", "")).strip()
        token = str(d.get("token", "")).strip()
        env = "sandbox" if d.get("env") == "sandbox" else "prod"
        if not RE_APNS_TOKEN.match(token):
            raise AppError("Ungültiger Push-Token.")
        with lock:
            m = mobiles()
            key = next((k for k in m if k.lower() == name.lower()), None)
            if key is None:
                raise AppError(f"{name or 'Das Gerät'} ist nicht als Mobilgerät angelegt.")
            m[key]["apns"] = {"token": token.lower(), "env": env, "set": time.strftime("%Y-%m-%d %H:%M")}
            mobile_save(m)
        return jsonify(ok=True)

    def queue_push(token: str, env: str, payload: dict, collapse: str = "", alert: bool = True) -> None:
        """Auftrag für den push-relay (gemeinsames Volume): er verschickt ihn per APNs und löscht die Datei."""
        job = {"time": time.time(), "token": token, "env": env, "payload": payload, "collapse": collapse,
               "type": "alert" if alert else "background"}
        _write(outbox / f"{time.time_ns()}-{secrets.token_hex(3)}.json", json.dumps(job), 0o600)

    def notify_held(kind: str, number: str, d: dict, holder: str) -> None:
        """sharedline: auf einem anderen Gerät gehalten -> Mitteilung „Übernehmen“; fortgesetzt -> wieder entfernen."""
        targets = [v["apns"] for v in mobiles().values()
                   if tail(v.get("number", "")) == tail(number) and v.get("apns", {}).get("token")]
        if not targets:
            return
        call_id = d.get("call_id", "")
        collapse = "held-" + re.sub(r"[^\w-]", "", call_id)[:40]
        if kind == "held":
            other = d.get("remote_uri", "").removeprefix("sip:").split("@")[0]
            title = d.get("remote_name") or other or "Gespräch"
            payload = {
                "aps": {"alert": {"title": title, "body": "Gehalten – tippen zum Übernehmen"},
                        "sound": "default", "category": "HELD_CALL", "thread-id": "held"},
                "held": {"callId": call_id, "localTag": d.get("local_tag", ""), "remoteTag": d.get("remote_tag", ""),
                         "remoteURI": d.get("remote_uri", ""), "remoteName": d.get("remote_name", ""),
                         "holder": holder},
            }
            for t in targets:
                queue_push(t["token"], t.get("env", "prod"), payload, collapse)
            print(f"Softphone: gehalten {title} ({number}) -> Mitteilung an {len(targets)} Gerät(e)", flush=True)
        else:
            payload = {"aps": {"content-available": 1}, "heldEnd": {"callId": call_id}}
            for t in targets:
                queue_push(t["token"], t.get("env", "prod"), payload, "", alert=False)

    # fuer sharedline.py: Mobilgeraete-Pools anmelden, Passwoerter der Apps pruefen, gehaltene Gespräche melden
    app.config["PHONEAPP_MOBILE"] = {"prefix": MOBILE_PREFIX, "pools": app_pools, "credentials": device_credentials,
                                     "account": mobile_account, "notify_held": notify_held}

    if not demo:
        write_flexisip_passwd()
        try:
            ensure()
        except OSError as e:
            print(f"Softphone: {data} nicht beschreibbar ({e})", flush=True)
