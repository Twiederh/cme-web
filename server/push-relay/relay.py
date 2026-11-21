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
  HTTP  :8090  GET/POST /push   (von Flexisip, nur an 127.0.0.1 binden)
               GET      /health
               GET      /devices (Diagnose: verbundene Local-Push-Geräte)
  TCP   :5070  Local-Push-Kanal (zeilenbasiertes JSON, siehe Shared/SharedConfig.swift)

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

import httpx
import jwt
from aiohttp import web

log = logging.getLogger("relay")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


@dataclass
class Config:
    http_host: str = os.getenv("RELAY_HTTP_HOST", "127.0.0.1")
    http_port: int = int(os.getenv("RELAY_HTTP_PORT", "8090"))
    lp_host: str = os.getenv("RELAY_LOCALPUSH_HOST", "0.0.0.0")
    lp_port: int = int(os.getenv("RELAY_LOCALPUSH_PORT", "5070"))
    lp_secret: str = os.getenv("RELAY_LOCALPUSH_SECRET", "")
    apns_key_path: str = os.getenv("APNS_KEY_PATH", "/run/secrets/apns.p8")
    apns_key_id: str = os.getenv("APNS_KEY_ID", "")
    apns_team_id: str = os.getenv("APNS_TEAM_ID", "")
    apns_bundle_id: str = os.getenv("APNS_BUNDLE_ID", "de.wiederhol.cmephone")
    # "auto": Umgebung aus Flexisip-Parametern (apns.dev / apns) ableiten
    apns_env: str = os.getenv("APNS_ENV", "auto")
    # Wie lange auf ein ACK der Extension gewartet wird, bevor zusätzlich APNs probiert wird
    lp_ack_timeout: float = float(os.getenv("RELAY_LOCALPUSH_ACK_TIMEOUT", "2.0"))


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
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jwt: tuple[float, str] | None = None
        self._key: str | None = None
        self.client = httpx.AsyncClient(http2=True, timeout=10)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.apns_key_id and self.cfg.apns_team_id and os.path.exists(self.cfg.apns_key_path))

    def _token(self) -> str:
        now = time.time()
        if self._jwt and now - self._jwt[0] < 45 * 60:   # Apple: 20–60 min gültig
            return self._jwt[1]
        if self._key is None:
            with open(self.cfg.apns_key_path) as f:
                self._key = f.read()
        tok = jwt.encode(
            {"iss": self.cfg.apns_team_id, "iat": int(now)},
            self._key,
            algorithm="ES256",
            headers={"kid": self.cfg.apns_key_id},
        )
        self._jwt = (now, tok)
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

        env = cfg.apns_env
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
        log.warning("APNs nicht konfiguriert (APNS_KEY_ID/APNS_TEAM_ID/%s) – nur Local Push aktiv", cfg.apns_key_path)

    lp_server = await asyncio.start_server(hub.handle, cfg.lp_host, cfg.lp_port)
    runner = web.AppRunner(build_app(cfg, hub, apns))
    await runner.setup()
    await web.TCPSite(runner, cfg.http_host, cfg.http_port).start()
    log.info("HTTP %s:%s, Local Push %s:%s", cfg.http_host, cfg.http_port, cfg.lp_host, cfg.lp_port)
    async with lp_server:
        await lp_server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
