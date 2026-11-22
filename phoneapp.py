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
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_KEYID = re.compile(r"^[A-Z0-9]{10}$")
RE_EXT = re.compile(r"^\+?[0-9]{1,32}$")
RE_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,253}$")
RE_REC_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
ENVS = ("auto", "sandbox", "production")
DEVICE_PREFIX = "CMEP"          # Geraetename der App am Router (wie cme_web.SOFTPHONE_PREFIX)


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

    def ensure() -> dict:
        """App-Schluessel und Local-Push-Secret beim ersten Mal erzeugen."""
        with lock:
            s = settings()
            if not s.get("token"):
                s["token"] = secrets.token_urlsafe(24)
                save(s)
            try:
                if not secret_path.exists():
                    _write(secret_path, (os.environ.get("RELAY_LOCALPUSH_SECRET", "").strip()
                                         or secrets.token_hex(24)) + "\n", 0o600)
            except OSError:
                pass
            return s

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
            ssids=s.get("ssids", ""), cme_host=os.environ.get("CME_HOST", ""), prefix=DEVICE_PREFIX)

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
            else:
                raise AppError("Unbekannt.")
        return jsonify(ok=True)

    # ---------------- Client-Schnittstelle fuer die Apps ----------------

    def client_auth() -> None:
        tok = settings().get("token", "")
        got = request.headers.get("Authorization", "")
        got = got[7:].strip() if got.lower().startswith("bearer ") else ""
        if not tok or not got or not secrets.compare_digest(got, tok):
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

    @app.get("/client/v1/info")
    def client_info():
        ext = ext_arg()
        s = settings()
        r = rec()
        # SIP-Domain der Apps: ein Name (Flexisip nimmt Anmeldungen mit fremder IP-Domain nicht an),
        # Flexisip leitet ihn per Route an CME_HOST weiter
        return jsonify(ok=True, ext=ext, device_name=DEVICE_PREFIX + ext.lstrip("+"),
                       cme_host=os.environ.get("SIP_DOMAIN", "").strip() or os.environ.get("CME_HOST", ""),
                       gateway={"host": s["gateway_host"], "port": s["gateway_port"], "transport": "tcp"},
                       localpush={"host": s["gateway_host"], "port": s["localpush_port"], "secret": lp_secret(),
                                  "ssids": [x for x in s.get("ssids", "").split(",") if x]},
                       voicemail=pilot(),
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

    if not demo:
        try:
            ensure()
        except OSError as e:
            print(f"Softphone: {data} nicht beschreibbar ({e})", flush=True)
