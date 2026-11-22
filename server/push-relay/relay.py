"""
cme-push-relay
==============

Kleiner Dienst neben Flexisip. Flexisip ruft für jeden Anruf an ein schlafendes iPhone/iPad
die `external-push-uri` auf (statt selbst mit APNs zu sprechen). Der Relay entscheidet dann:

  1. Ist das Gerät per Local-Push-Extension verbunden (= im Heim-WLAN)?
     -> Anruf direkt über die TCP-Verbindung melden (kein Internet / APNs nötig).
  2. Sonst -> VoIP-Push (PushKit) über APNs (HTTP/2, Token-Auth mit .p8-Schlüssel).

Endpunkte
---------
  HTTP  :8092  GET/POST /push   (von Flexisip, nur an 127.0.0.1 binden)
               GET      /health
               GET      /devices (Diagnose: verbundene Local-Push-Geräte)
  TCP   :5072  Local-Push-Kanal (zeilenbasiertes JSON, siehe Shared/SharedConfig.swift)
  HTTP  :8091  Verwaltung (Basic-Auth, nur wenn RELAY_ADMIN_PASSWORD gesetzt):
               APNs-Schlüssel (.p8) hochladen, Key-ID/Team-ID/Umgebung setzen, Status

Konfiguration über Umgebungsvariablen (siehe docker-compose.yml).
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


@dataclass
class Config:
    http_host: str = os.getenv("RELAY_HTTP_HOST", "127.0.0.1")
    http_port: int = int(os.getenv("RELAY_HTTP_PORT", "8092"))
    lp_host: str = os.getenv("RELAY_LOCALPUSH_HOST", "0.0.0.0")
    lp_port: int = int(os.getenv("RELAY_LOCALPUSH_PORT", "5072"))
    lp_secret: str = os.getenv("RELAY_LOCALPUSH_SECRET", "")
    apns_key_path: str = os.getenv("APNS_KEY_PATH", "/run/secrets/apns.p8")
    apns_key_id: str = os.getenv("APNS_KEY_ID", "")
    apns_team_id: str = os.getenv("APNS_TEAM_ID", "")
    apns_bundle_id: str = os.getenv("APNS_BUNDLE_ID", "de.wiederhol.cmephone")
    # "auto": Umgebung aus Flexisip-Parametern (apns.dev / apns) ableiten
    apns_env: str = os.getenv("APNS_ENV", "auto")
    # Wie lange auf ein ACK der Extension gewartet wird, bevor zusätzlich APNs probiert wird
    lp_ack_timeout: float = float(os.getenv("RELAY_LOCALPUSH_ACK_TIMEOUT", "2.0"))
    # Verwaltungsoberfläche (Schlüssel-Upload). Leeres Passwort = abgeschaltet.
    admin_host: str = os.getenv("RELAY_ADMIN_HOST", "0.0.0.0")
    admin_port: int = int(os.getenv("RELAY_ADMIN_PORT", "8091"))
    admin_user: str = os.getenv("RELAY_ADMIN_USER", "admin")
    admin_password: str = os.getenv("RELAY_ADMIN_PASSWORD", "")
    # Persistenter Speicher für hochgeladene Schlüssel (Docker-Volume)
    data_dir: str = os.getenv("RELAY_DATA_DIR", "/data")


# ---------------------------------------------------------------------------
# APNs-Zugangsdaten: hochgeladen (Volume) oder aus Umgebungsvariablen
# ---------------------------------------------------------------------------

ID_RE = re.compile(r"^[A-Z0-9]{10}$")


class KeyError_(ValueError):
    pass


def validate_p8(pem: bytes) -> str:
    """Prüft, dass es ein EC-P-256-Schlüssel im PEM-Format ist (wie Apple ihn liefert)."""
    text = pem.decode("utf-8", errors="strict").strip()
    if "BEGIN PRIVATE KEY" not in text:
        raise KeyError_("Keine .p8-Datei (PEM 'BEGIN PRIVATE KEY' fehlt)")
    try:
        key = load_pem_private_key(text.encode(), password=None)
    except Exception as e:  # noqa: BLE001
        raise KeyError_(f"Schlüssel nicht lesbar: {e}") from e
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise KeyError_("Kein APNs-Schlüssel (erwartet EC P-256)")
    return text + "\n"


class KeyStore:
    """Liefert Key-ID, Team-ID, Umgebung und PEM. Hochgeladene Daten haben Vorrang vor ENV."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = Path(cfg.data_dir) / "apns"
        self.meta_path = self.dir / "apns.json"
        self.key_path = self.dir / "AuthKey.p8"
        self.version = 0          # erhöht sich bei jeder Änderung (APNs-Client verwirft dann sein JWT)

    def _meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {}

    @property
    def source(self) -> str:
        if self.key_path.exists() and self._meta().get("key_id"):
            return "upload"
        if self.cfg.apns_key_id and self.cfg.apns_team_id and os.path.exists(self.cfg.apns_key_path):
            return "env"
        return "none"

    def current(self) -> dict | None:
        src = self.source
        if src == "upload":
            m = self._meta()
            return {"key_id": m["key_id"], "team_id": m["team_id"], "env": m.get("env", "auto"),
                    "pem": self.key_path.read_text(), "source": src, "uploaded": m.get("uploaded")}
        if src == "env":
            with open(self.cfg.apns_key_path) as f:
                pem = f.read()
            return {"key_id": self.cfg.apns_key_id, "team_id": self.cfg.apns_team_id, "env": self.cfg.apns_env,
                    "pem": pem, "source": src, "uploaded": None}
        return None

    def save(self, pem: bytes | None, key_id: str, team_id: str, env: str) -> None:
        key_id, team_id = key_id.strip().upper(), team_id.strip().upper()
        if not ID_RE.match(key_id):
            raise KeyError_("Key-ID muss aus 10 Zeichen A–Z/0–9 bestehen")
        if not ID_RE.match(team_id):
            raise KeyError_("Team-ID muss aus 10 Zeichen A–Z/0–9 bestehen")
        if env not in ("auto", "sandbox", "production"):
            raise KeyError_("Umgebung muss auto, sandbox oder production sein")
        self.dir.mkdir(parents=True, exist_ok=True)
        if pem is not None:
            text = validate_p8(pem)
            tmp = self.key_path.with_suffix(".tmp")
            tmp.write_text(text)
            os.chmod(tmp, 0o600)
            tmp.replace(self.key_path)
        elif not self.key_path.exists():
            raise KeyError_("Bitte die .p8-Datei auswählen")
        meta = {"key_id": key_id, "team_id": team_id, "env": env,
                "uploaded": time.strftime("%Y-%m-%d %H:%M:%S")}
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(self.meta_path)
        self.version += 1
        log.info("APNs-Schlüssel %s (Team %s, %s) gespeichert", key_id, team_id, env)

    def delete(self) -> None:
        for p in (self.key_path, self.meta_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.version += 1
        log.info("Hochgeladener APNs-Schlüssel gelöscht")


# ---------------------------------------------------------------------------
# Token-Normalisierung
# ---------------------------------------------------------------------------

HEX_RE = re.compile(r"[0-9a-fA-F]{32,}")


def voip_token(raw: str) -> str | None:
    """Extrahiert den VoIP-Token (hex) aus den verschiedenen Formaten.

    Flexisip/liblinphone (RFC 8599):  pn-prid = "<remote>:remote&<voip>:voip" oder "<voip>:voip"
    App/Extension:                      "<hex>" oder "<hex>:voip"
    """
    if not raw:
        return None
    raw = raw.strip()
    parts = [p for p in re.split(r"[&]", raw) if p]
    for p in parts:
        if p.endswith(":voip"):
            m = HEX_RE.search(p)
            if m:
                return m.group(0).lower()
    m = HEX_RE.search(raw)
    return m.group(0).lower() if m else None


# ---------------------------------------------------------------------------
# Local Push
# ---------------------------------------------------------------------------


@dataclass
class LocalDevice:
    token: str
    writer: asyncio.StreamWriter
    peer: str
    since: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    acks: dict[str, asyncio.Future] = field(default_factory=dict)


class LocalPushHub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.devices: dict[str, LocalDevice] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = "%s:%s" % (writer.get_extra_info("peername") or ("?", 0))[:2]
        dev: LocalDevice | None = None
        log.info("local-push: Verbindung von %s", peer)
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=180)
                if not line:
                    break
                if len(line) > 16384:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = msg.get("type")
                if t == "hello":
                    if self.cfg.lp_secret and not secrets.compare_digest(
                        str(msg.get("secret", "")), self.cfg.lp_secret
                    ):
                        await self._send(writer, {"type": "error", "reason": "bad secret"})
                        log.warning("local-push: falsches Secret von %s", peer)
                        break
                    tok = voip_token(str(msg.get("token", "")))
                    if not tok:
                        await self._send(writer, {"type": "error", "reason": "no token"})
                        break
                    old = self.devices.get(tok)
                    if old and old.writer is not writer:
                        old.writer.close()
                    dev = LocalDevice(token=tok, writer=writer, peer=peer)
                    self.devices[tok] = dev
                    await self._send(writer, {"type": "ok"})
                    log.info("local-push: Gerät %s… angemeldet (%s)", tok[:10], peer)
                elif t == "ping":
                    if dev:
                        dev.last_seen = time.time()
                    await self._send(writer, {"type": "pong"})
                elif t == "ack" and dev:
                    fut = dev.acks.pop(str(msg.get("call-id", "")), None)
                    if fut and not fut.done():
                        fut.set_result(True)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if dev and self.devices.get(dev.token) is dev:
                del self.devices[dev.token]
                log.info("local-push: Gerät %s… getrennt", dev.token[:10])
            writer.close()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, obj: dict):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def notify(self, token: str, call: dict) -> bool:
        """True, wenn die Extension den Anruf bestätigt hat."""
        dev = self.devices.get(token)
        if not dev:
            return False
        fut = asyncio.get_running_loop().create_future()
        dev.acks[call["call-id"]] = fut
        try:
            await self._send(dev.writer, {"type": "call", **call})
            await asyncio.wait_for(fut, timeout=self.cfg.lp_ack_timeout)
            return True
        except (asyncio.TimeoutError, ConnectionError) as e:
            log.warning("local-push: keine Bestätigung von %s… (%s)", token[:10], type(e).__name__)
            dev.acks.pop(call["call-id"], None)
            return False


# ---------------------------------------------------------------------------
# APNs
# ---------------------------------------------------------------------------


class APNs:
    def __init__(self, cfg: Config, keys: "KeyStore | None" = None):
        self.cfg = cfg
        self.keys = keys or KeyStore(cfg)
        self._jwt: tuple[float, int, str] | None = None   # (Zeit, KeyStore-Version, Token)
        self.last_result: str = ""
        self.client = httpx.AsyncClient(http2=True, timeout=10)

    @property
    def configured(self) -> bool:
        try:
            return self.keys.current() is not None
        except OSError:
            return False

    @property
    def env(self) -> str:
        cur = self.keys.current()
        return cur["env"] if cur else self.cfg.apns_env

    def _token(self) -> str:
        now = time.time()
        if self._jwt and self._jwt[1] == self.keys.version and now - self._jwt[0] < 45 * 60:   # Apple: 20–60 min
            return self._jwt[2]
        cur = self.keys.current()
        if cur is None:
            raise RuntimeError("APNs nicht konfiguriert")
        tok = jwt.encode(
            {"iss": cur["team_id"], "iat": int(now)},
            cur["pem"],
            algorithm="ES256",
            headers={"kid": cur["key_id"]},
        )
        self._jwt = (now, self.keys.version, tok)
        return tok

    async def send_voip(self, token: str, call: dict, sandbox: bool) -> tuple[int, str]:
        host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
        # Payload im Format, das liblinphone erwartet (wie Flexisip es selbst sendet):
        # "call-id" auf oberster Ebene -> Core.processPushNotification.
        payload = {
            "aps": {"sound": "", "loc-key": "IC_MSG", "loc-args": [call.get("display-name") or call.get("from-uri", "")]},
            "call-id": call["call-id"],
            "from-uri": call.get("from-uri", ""),
            "display-name": call.get("display-name", ""),
            "pn_ttl": 30,
            "uuid": call.get("uuid", ""),
            "send-time": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        r = await self.client.post(
            f"https://{host}/3/device/{token}",
            json=payload,
            headers={
                "authorization": f"bearer {self._token()}",
                "apns-topic": f"{self.cfg.apns_bundle_id}.voip",
                "apns-push-type": "voip",
                "apns-priority": "10",
                "apns-expiration": "0",
            },
        )
        return r.status_code, r.text


# ---------------------------------------------------------------------------
# HTTP (Flexisip external-push-uri)
# ---------------------------------------------------------------------------


def build_app(cfg: Config, hub: LocalPushHub, apns: APNs) -> web.Application:
    app = web.Application()

    async def push(request: web.Request) -> web.Response:
        q = dict(request.query)
        if request.method == "POST" and request.can_read_body:
            try:
                body = await request.post()
                q.update({k: str(v) for k, v in body.items()})
            except Exception:  # noqa: BLE001
                pass
        log.info("push-request: %s", {k: (v[:16] + "…" if k == "token" and len(v) > 16 else v) for k, v in q.items()})

        token = voip_token(q.get("token", "") or q.get("pn-prid", ""))
        if not token:
            return web.json_response({"error": "no voip token"}, status=400)

        call = {
            "call-id": q.get("call-id") or str(uuid.uuid4()),
            "from-uri": q.get("from-uri", ""),
            "display-name": q.get("from-name", "") or q.get("display-name", ""),
            "uuid": q.get("uid", ""),
            "video": False,
        }

        # 1) Local Push
        if await hub.notify(token, call):
            log.info("Anruf %s per Local Push zugestellt", call["call-id"][:12])
            return web.json_response({"delivered": "local-push"})

        # 2) APNs
        if not apns.configured:
            log.error("APNs nicht konfiguriert – Anruf %s nicht zustellbar", call["call-id"][:12])
            return web.json_response({"error": "apns not configured"}, status=503)

        env = apns.env
        if env == "auto":
            hint = (q.get("type", "") + q.get("app-id", "") + q.get("pn-provider", "")).lower()
            sandbox = ".dev" in hint or "dev" == q.get("env", "")
        else:
            sandbox = env == "sandbox"
        status, text = await apns.send_voip(token, call, sandbox)
        log.info("APNs (%s) -> %s %s", "sandbox" if sandbox else "prod", status, text[:200])
        if status == 400 and "BadDeviceToken" in text and env == "auto":
            # Token gehört zur anderen Umgebung (Debug-Build vs. TestFlight/Ad-hoc)
            sandbox = not sandbox
            status, text = await apns.send_voip(token, call, sandbox)
            log.info("APNs-Retry (%s) -> %s %s", "sandbox" if sandbox else "prod", status, text[:200])
        apns.last_result = f"{time.strftime('%H:%M:%S')} {'sandbox' if sandbox else 'production'}: {status} {text[:120]}"
        if status == 200:
            return web.json_response({"delivered": "apns"})
        return web.json_response({"error": "apns", "status": status, "body": text}, status=502)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "local_devices": len(hub.devices), "apns": apns.configured})

    async def devices(_: web.Request) -> web.Response:
        return web.json_response(
            [
                {"token": d.token[:10] + "…", "peer": d.peer, "since": int(d.since), "last_seen": int(d.last_seen)}
                for d in hub.devices.values()
            ]
        )

    app.router.add_route("GET", "/push", push)
    app.router.add_route("POST", "/push", push)
    app.router.add_get("/health", health)
    app.router.add_get("/devices", devices)
    return app


# ---------------------------------------------------------------------------
# Verwaltung (Schlüssel-Upload)
# ---------------------------------------------------------------------------

ADMIN_CSS = """
body{font:15px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;color:#1d1d1f}
h1{font-size:22px}h2{font-size:17px;margin-top:28px}
table{border-collapse:collapse;width:100%}td{padding:6px 8px;border-bottom:1px solid #e5e5e5;vertical-align:top}
td:first-child{color:#6e6e73;width:38%}
label{display:block;margin:12px 0 4px;font-weight:600}
input[type=text],select{width:100%;padding:8px;border:1px solid #c7c7cc;border-radius:8px;font:inherit;box-sizing:border-box}
button{margin-top:16px;padding:9px 16px;border:0;border-radius:8px;background:#0071e3;color:#fff;font:inherit;cursor:pointer}
button.danger{background:#d70015}.ok{color:#1a7f37}.bad{color:#d70015}.msg{padding:10px;border-radius:8px;background:#f2f2f7}
small{color:#6e6e73}
"""


def build_admin_app(cfg: Config, hub: LocalPushHub, apns: APNs) -> web.Application:
    csrf = secrets.token_urlsafe(24)

    @web.middleware
    async def auth(request: web.Request, handler):
        hdr = request.headers.get("Authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, cfg.admin_user) and secrets.compare_digest(pw, cfg.admin_password)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="cme-push-relay"'},
                                text="Anmeldung erforderlich")
        return await handler(request)

    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)

    def status() -> dict:
        cur = None
        try:
            cur = apns.keys.current()
        except OSError as e:
            log.warning("APNs-Schlüssel nicht lesbar: %s", e)
        return {
            "apns": cur is not None,
            "source": apns.keys.source,
            "key_id": cur["key_id"] if cur else None,
            "team_id": cur["team_id"] if cur else None,
            "env": cur["env"] if cur else None,
            "uploaded": cur["uploaded"] if cur else None,
            "last_apns_result": apns.last_result,
            "local_devices": [
                {"token": d.token[:10] + "…", "peer": d.peer, "since": int(d.since)} for d in hub.devices.values()
            ],
        }

    def page(message: str = "", ok: bool = True) -> web.Response:
        st = status()
        e = html.escape
        src = {"upload": "hochgeladen", "env": "Umgebungsvariablen / Datei", "none": "–"}[st["source"]]
        state = '<span class="ok">aktiv</span>' if st["apns"] else '<span class="bad">nicht eingerichtet</span>'
        devs = "".join(f"<tr><td>{e(d['token'])}</td><td>{e(d['peer'])}</td></tr>" for d in st["local_devices"]) \
            or "<tr><td colspan=2><small>keine</small></td></tr>"
        env_opts = "".join(
            f'<option value="{v}"{" selected" if (st["env"] or "auto") == v else ""}>{t}</option>'
            for v, t in (("auto", "auto (Sandbox und Production)"), ("sandbox", "nur Sandbox (Debug-Builds)"),
                         ("production", "nur Production (TestFlight/Ad-hoc)")))
        msg = f'<p class="msg {"ok" if ok else "bad"}">{e(message)}</p>' if message else ""
        delete = f"""<form method="post" action="delete" onsubmit="return confirm('Hochgeladenen Schlüssel löschen?')">
<input type="hidden" name="csrf" value="{csrf}"><button class="danger">Hochgeladenen Schlüssel löschen</button></form>""" \
            if st["source"] == "upload" else ""
        body = f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><title>CME Phone – Push-Relay</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>{ADMIN_CSS}</style></head><body>
<h1>CME Phone – Push-Relay</h1>{msg}
<h2>APNs</h2><table>
<tr><td>Status</td><td>{state}</td></tr>
<tr><td>Quelle</td><td>{src}</td></tr>
<tr><td>Key-ID</td><td>{e(st["key_id"] or "–")}</td></tr>
<tr><td>Team-ID</td><td>{e(st["team_id"] or "–")}</td></tr>
<tr><td>Umgebung</td><td>{e(st["env"] or "–")}</td></tr>
<tr><td>Hochgeladen</td><td>{e(st["uploaded"] or "–")}</td></tr>
<tr><td>Letzter APNs-Versand</td><td>{e(st["last_apns_result"] or "–")}</td></tr>
</table>
<h2>Schlüssel hochladen</h2>
<form method="post" action="apns" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="{csrf}">
<label for="f">.p8-Datei (AuthKey_XXXXXXXXXX.p8)</label>
<input id="f" type="file" name="keyfile" accept=".p8" onchange="var m=this.files[0]&&this.files[0].name.match(/AuthKey_([A-Z0-9]{{10}})\\.p8/i);if(m)document.getElementById('k').value=m[1].toUpperCase()">
<small>Leer lassen, um nur Key-ID/Team-ID/Umgebung zu ändern.</small>
<label for="k">Key-ID</label><input id="k" type="text" name="key_id" maxlength="10" value="{e(st["key_id"] or "")}" required>
<label for="t">Team-ID</label><input id="t" type="text" name="team_id" maxlength="10" value="{e(st["team_id"] or "83BLV56527")}" required>
<label for="e">Umgebung</label><select id="e" name="env">{env_opts}</select>
<button>Speichern</button></form>
{delete}
<h2>Verbundene Geräte (Local Push)</h2><table>{devs}</table>
</body></html>"""
        return web.Response(text=body, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def index(_: web.Request) -> web.Response:
        return page()

    async def upload(request: web.Request) -> web.Response:
        form = await request.post()
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf):
            return page("Formular abgelaufen – bitte neu laden.", ok=False)
        f = form.get("keyfile")
        pem = f.file.read(16 * 1024) if getattr(f, "file", None) is not None and getattr(f, "filename", "") else None
        try:
            apns.keys.save(pem, str(form.get("key_id", "")), str(form.get("team_id", "")), str(form.get("env", "auto")))
            # Testweise ein JWT erzeugen, damit Fehler sofort auffallen
            apns._token()
        except KeyError_ as e:
            return page(str(e), ok=False)
        except Exception as e:  # noqa: BLE001
            log.exception("Upload fehlgeschlagen")
            return page(f"Fehler: {e}", ok=False)
        return page("Gespeichert. Der Schlüssel ist sofort aktiv.")

    async def delete(request: web.Request) -> web.Response:
        form = await request.post()
        if not secrets.compare_digest(str(form.get("csrf", "")), csrf):
            return page("Formular abgelaufen – bitte neu laden.", ok=False)
        apns.keys.delete()
        return page("Hochgeladener Schlüssel gelöscht.")

    async def api_status(_: web.Request) -> web.Response:
        return web.json_response(status())

    app.router.add_get("/", index)
    app.router.add_post("/apns", upload)
    app.router.add_post("/delete", delete)
    app.router.add_get("/api/status", api_status)
    return app


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = Config()
    hub = LocalPushHub(cfg)
    apns = APNs(cfg)

    if not cfg.lp_secret:
        log.warning("RELAY_LOCALPUSH_SECRET ist leer – jeder im Netz kann sich als Gerät anmelden!")
    if not apns.configured:
        log.warning("APNs nicht konfiguriert – Schlüssel über die Verwaltung (Port %s) hochladen "
                    "oder APNS_KEY_ID/APNS_TEAM_ID/%s setzen. Bis dahin nur Local Push.", cfg.admin_port, cfg.apns_key_path)

    lp_server = await asyncio.start_server(hub.handle, cfg.lp_host, cfg.lp_port)
    runner = web.AppRunner(build_app(cfg, hub, apns))
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("HTTP %s:%s, Local Push %s:%s", cfg.http_host, cfg.http_port, cfg.lp_host, cfg.lp_port)
    if cfg.admin_password:
        admin = web.AppRunner(build_admin_app(cfg, hub, apns))
        await admin.setup()
        await web.TCPSite(admin, cfg.admin_host, cfg.admin_port).start()
        log.info("Verwaltung auf http://%s:%s/ (Benutzer %s)", cfg.admin_host, cfg.admin_port, cfg.admin_user)
    else:
        log.info("Verwaltung aus (RELAY_ADMIN_PASSWORD nicht gesetzt)")
    async with lp_server:
        await lp_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
