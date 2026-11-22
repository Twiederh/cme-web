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
  TCP   :5072  Local-Push-Kanal (zeilenbasiertes JSON, siehe softphone/Shared/SharedConfig.swift)

Verwaltet wird der Relay in cme-web (Tab "Softphone"). Beide teilen sich das Volume RELAY_DATA_DIR:
  apns/AuthKey.p8, apns/apns.json   APNs-Schlüssel (schreibt cme-web, liest der Relay bei jeder Änderung neu)
  localpush-secret                  Secret für den Local-Push-Kanal (schreibt cme-web; sonst RELAY_LOCALPUSH_SECRET)
  status.json                       Lebenszeichen, verbundene Geräte, letzter APNs-Versand (schreibt der Relay)

Konfiguration über Umgebungsvariablen (siehe docker-compose.yml).
"""
from __future__ import annotations

import asyncio
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
    # Gemeinsames Volume mit cme-web (Schlüssel, Secret, Status)
    data_dir: str = os.getenv("RELAY_DATA_DIR", "/data")

    def secret(self) -> str:
        """Local-Push-Secret: aus cme-web (Datei im Volume), sonst aus der Umgebung."""
        try:
            v = (Path(self.data_dir) / "localpush-secret").read_text().strip()
            if v:
                return v
        except OSError:
            pass
        return self.lp_secret


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
        self._bump = 0

    @property
    def version(self) -> tuple:
        """Ändert sich mit jeder Änderung der Dateien (auch durch cme-web) -> APNs-Client erneuert sein JWT."""
        def mt(p: Path) -> float:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0
        return (mt(self.key_path), mt(self.meta_path), self._bump)

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
        self._bump += 1
        log.info("APNs-Schlüssel %s (Team %s, %s) gespeichert", key_id, team_id, env)

    def delete(self) -> None:
        for p in (self.key_path, self.meta_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self._bump += 1
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
        self.last_push: dict = {}

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
                    secret = self.cfg.secret()
                    if secret and not secrets.compare_digest(str(msg.get("secret", "")), secret):
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
        self._jwt: tuple[float, tuple, str] | None = None   # (Zeit, KeyStore-Version, Token)
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

        hub.last_push = {"time": int(time.time()), "from": call["display-name"] or call["from-uri"], "via": ""}

        # 1) Local Push
        if await hub.notify(token, call):
            hub.last_push["via"] = "local-push"
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
        hub.last_push["via"] = f"apns {status}"
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
# Status für cme-web (Datei im gemeinsamen Volume)
# ---------------------------------------------------------------------------


def status_dict(cfg: Config, hub: LocalPushHub, apns: APNs) -> dict:
    cur = None
    try:
        cur = apns.keys.current()
    except OSError:
        pass
    return {
        "time": time.time(),
        "apns": {"configured": cur is not None, "source": apns.keys.source,
                 "key_id": cur["key_id"] if cur else "", "team_id": cur["team_id"] if cur else "",
                 "env": cur["env"] if cur else cfg.apns_env, "bundle_id": cfg.apns_bundle_id,
                 "last_result": apns.last_result},
        "localpush": {"port": cfg.lp_port, "secret_set": bool(cfg.secret()),
                      "devices": [{"token": d.token[:10] + "…", "peer": d.peer, "since": int(d.since),
                                   "last_seen": int(d.last_seen)} for d in hub.devices.values()]},
        "last_push": hub.last_push,
    }


async def write_status(cfg: Config, hub: LocalPushHub, apns: APNs, every: float = 5.0) -> None:
    path = Path(cfg.data_dir) / "status.json"
    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status_dict(cfg, hub, apns)))
            tmp.replace(path)
        except OSError as e:
            log.warning("status.json nicht schreibbar: %s", e)
        await asyncio.sleep(every)


async def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = Config()
    hub = LocalPushHub(cfg)
    apns = APNs(cfg)

    if not cfg.secret():
        log.warning("Kein Local-Push-Secret – jeder im Netz kann sich als Gerät anmelden! (cme-web, Tab Softphone)")
    if not apns.configured:
        log.warning("APNs nicht konfiguriert – Schlüssel in cme-web (Tab Softphone) hochladen. Bis dahin nur Local Push.")

    lp_server = await asyncio.start_server(hub.handle, cfg.lp_host, cfg.lp_port)
    runner = web.AppRunner(build_app(cfg, hub, apns))
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("HTTP %s:%s, Local Push %s:%s", cfg.http_host, cfg.http_port, cfg.lp_host, cfg.lp_port)
    asyncio.create_task(write_status(cfg, hub, apns))
    async with lp_server:
        await lp_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
