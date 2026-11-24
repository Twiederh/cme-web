"""Shared Line für Geräte, die die Leitung nicht selbst abonnieren (z. B. Gigaset-DECT-Basis).

CME klingelt eine Shared Line (voice register dn … shared-line) nur an den Geräten an, die die Leitung per
SUBSCRIBE Event: dialog abonniert haben ("show shared-line details"). Den Teilnehmer führt CME unter dem
From-URI des Abos und sucht seinen Dial-Peer über den Host darin – das Abo muss also nicht vom Gerät selbst
kommen, es muss nur dessen IP im From tragen. Die INVITEs schickt CME dann direkt an das Gerät.

Cisco-Telefone und CME Phone abonnieren selbst. Für jedes andere angemeldete Gerät auf einer Shared Line, das
CME nicht als Teilnehmer führt, hält dieser Dienst stellvertretend das Abo: SUBSCRIBE mit der Geräte-IP im
From, NOTIFYs bestätigen, rechtzeitig erneuern, beim Abmelden des Geräts beenden.

Mobilgeräte (iPhone/iPad, CME Phone) einer Rufnummer: Die Apps melden sich nur an Flexisip an (Push-Gateway).
Flexisip reicht ihre REGISTER hierher weiter; dieser Dienst prüft das Passwort des Pools MOB<Nummer> und
antwortet. Für CME meldet er EINEN Pool MOB<Nummer> mit Flexisip als Ziel an und hält dafür das Shared-Line-
Abo (From = IP von Flexisip). Klingelt die Nummer, schickt CME das INVITE an Flexisip, und Flexisip weckt
und klingelt alle iPhones/iPads der Nummer parallel an. Unaufgeforderte Anfragen der CME an Flexisip
(NOTIFY service-control, REFER, OPTIONS) leitet Flexisip ebenfalls hierher; sie werden bestätigt.

Braucht einen UDP-Port, den CME und Flexisip erreichen: SHARED_LINE_PORT (Standard 5074) auf dem Docker-Host
(VM_HOST_IP). Oberfläche: static/sharedline.js (Tab Telefone).
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import socket
import string
import threading
import time
from pathlib import Path

from flask import jsonify, request

EXPIRES = 600
# Anmeldungen der Apps (iPhone/iPad) bei Flexisip: im Hintergrund erneuert iOS sie nicht – sie müssen bis zum
# nächsten Öffnen der App gelten, sonst antwortet Flexisip auf Anrufe mit 404 und es gibt keinen Push.
APP_MAX_EXPIRES = 7 * 24 * 3600
CHECK_EVERY = 60          # so oft wird die Router-Konfiguration verglichen
MISSING_ROUNDS = 2        # erst abonnieren, wenn das Gerät so oft hintereinander als Teilnehmer fehlt
# Diese Geräte abonnieren selbst (Cisco-Telefone; Jabber/CME Phone haben Gerätenamen CSF…)
SELF_SUBSCRIBING = re.compile(r"^(\d{4}|CiscoMobile|CIPC)", re.I)
CISCO_SUPPORTED = ("replaces,join,sdp-anat,norefersub,resource-priority,extended-refer,X-cisco-callinfo,"
                   "X-cisco-serviceuri,X-cisco-escapecodes,X-cisco-service-control,X-cisco-srtp-fallback,"
                   "X-cisco-monrec,X-cisco-config,X-cisco-sis-7.0.0,X-cisco-xsi-8.5.1")


def _rnd(n: int = 10) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _header(msg: str, name: str) -> str:
    m = re.search(rf"^{name}\s*:\s*(.*)$", msg, re.I | re.M)
    return m.group(1).strip() if m else ""


def _headers(msg: str, name: str) -> list[str]:
    return [v.strip() for v in re.findall(rf"^{name}\s*:\s*(.*)$", msg, re.I | re.M)]


def _tail(n: str) -> str:
    d = re.sub(r"\D", "", n or "")
    return d[-8:] if len(d) >= 8 else d


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def digest_params(hdr: str) -> dict:
    return {k.lower(): (v1 if v1 else v2) for k, v1, v2 in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', hdr)}


def digest_response(p: dict, method: str, user: str, password: str, uri: str, cnonce: str = "", nc: str = "") -> str:
    ha1 = _md5(f"{user}:{p['realm']}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if "auth" in p.get("qop", ""):
        return _md5(f"{ha1}:{p['nonce']}:{nc}:{cnonce}:auth:{ha2}")
    return _md5(f"{ha1}:{p['nonce']}:{ha2}")


def authorization(challenge: str, method: str, uri: str, user: str, password: str) -> str:
    p = digest_params(challenge)
    if "auth" in p.get("qop", ""):
        cnonce, nc = _rnd(16), "00000001"
        resp = digest_response(p, method, user, password, uri, cnonce, nc)
        return (f'Digest username="{user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", '
                f'response="{resp}", algorithm=MD5, cnonce="{cnonce}", nc={nc}, qop=auth')
    resp = digest_response(p, method, user, password, uri)
    return f'Digest username="{user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", response="{resp}", algorithm=MD5'


def parse_shared_users(out: str) -> dict[str, set[str]]:
    """show shared-line details -> {"+4961…": {"192.168.11.46", …}}"""
    res: dict[str, set[str]] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^\s*Shared-Line:\s*'([^']+)'", line)
        if m:
            cur = res.setdefault(m.group(1), set())
            continue
        m = re.match(r"^\s*\d+\s+(\S+)@(\d+\.\d+\.\d+\.\d+)\s", line)
        if m and cur is not None:
            cur.add(m.group(2))
    return res


def wanted(dns: dict, pools: dict, brief: dict, users: dict[str, set[str]]) -> list[dict]:
    """Geräte auf Shared Lines, die nicht selbst abonnieren: [{number, ip, pool}]"""
    out = []
    for tag, p in pools.items():
        # Jabber/CME Phone (CSF…) abonnieren selbst; Mobilgeräte-Pools (MOB…) übernimmt dieser Dienst
        if SELF_SUBSCRIBING.match(p.get("type", "")) or p.get("device_id", "").upper().startswith("CSF"):
            continue
        b = brief.get(tag) or {}
        if b.get("state") != "REGISTERED" or not b.get("ip"):
            continue
        for ln in p.get("numbers", []):
            dn = dns.get(ln["dn"]) or {}
            if dn.get("shared") and dn.get("number"):
                out.append({"number": dn["number"], "ip": b["ip"], "pool": tag})
    return out


class Sub:
    def __init__(self, number: str, ip: str, pool: str):
        self.number, self.ip, self.pool = number, ip, pool
        self.call_id, self.tag = _rnd(16), _rnd(9)
        self.to_tag, self.cseq, self.target = "", 20, ""
        self.state, self.error, self.since, self.next = "neu", "", 0.0, 0.0

    @property
    def key(self) -> tuple[str, str]:
        return self.number, self.ip


class Reg:
    """Anmeldung eines Mobilgeräte-Pools an CME (Contact = Flexisip)."""

    def __init__(self, number: str, device: str, username: str, password: str):
        self.number, self.device, self.username, self.password = number, device, username, password
        self.call_id, self.tag, self.cseq = _rnd(16), _rnd(9), 20
        self.state, self.error, self.since, self.next = "neu", "", 0.0, 0.0


class Agent:
    def __init__(self, cme: str, host: str, port: int, flexisip: str = "", credentials=None, realm: str = "cme-web"):
        self.cme, self.host, self.port = cme, host, port
        self.flexisip = flexisip                      # "IP:Port" von Flexisip (Contact der Mobilgeräte-Pools)
        self.credentials = credentials or (lambda name: {})   # Mobilgerät: Name -> {number, password}
        self.realm = realm
        self.regs: dict[str, Reg] = {}
        self.nonces: dict[str, float] = {}
        self.app_regs: dict[str, float] = {}          # Nummer -> letzte erfolgreiche App-Anmeldung
        self.subs: dict[tuple[str, str], Sub] = {}
        self.missing: dict[tuple[str, str], int] = {}
        self.lock = threading.Lock()
        self.waiting: dict[tuple[str, int], tuple[threading.Event, list]] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        threading.Thread(target=self._recv_loop, daemon=True, name="sharedline-recv").start()

    # ---------- SIP ----------

    def _send(self, text: str) -> None:
        self.sock.sendto(text.encode(), (self.cme, 5060))

    def _reply_ok(self, req: str, addr) -> None:
        keep = [l for l in req.split("\r\n") if re.match(r"(Via|From|To|Call-ID|CSeq)\s*:", l, re.I)]
        self.sock.sendto("\r\n".join(["SIP/2.0 200 OK"] + keep + ["Content-Length: 0", "", ""]).encode(), addr)

    def _recv_loop(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                time.sleep(1)
                continue
            msg = data.decode(errors="replace")
            if msg.startswith("SIP/2.0"):
                m = re.match(r"(\d+)\s+(\w+)", _header(msg, "CSeq"))
                if m:
                    w = self.waiting.get((_header(msg, "Call-ID"), int(m.group(1))))
                    if w and not msg.split()[1].startswith("1"):
                        w[1].append(msg)
                        w[0].set()
            elif msg.startswith("REGISTER"):
                try:
                    self._app_register(msg, addr)
                except Exception:  # noqa: BLE001
                    self._reply(msg, addr, "500 Server Internal Error")
            elif msg.strip():
                # NOTIFY (Event: dialog, service-control), REFER, OPTIONS u. a. bestätigen – ohne die Bestätigung
                # des ersten NOTIFY nimmt CME das Gerät nicht in die Shared Line auf
                self._reply_ok(msg, addr)
                if msg.startswith("NOTIFY") and _header(msg, "Event").lower().startswith("dialog"):
                    self._log_dialog(msg)

    _last_dialog: dict = {}

    def _log_dialog(self, msg: str) -> None:
        """Diagnose: Inhalt der dialog-NOTIFYs der CME knapp ins Log (Gehalten/Übernehmen auf der Shared Line)."""
        body = msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""
        to = _header(msg, "To")
        if self._last_dialog.get(to) == body:
            return
        self._last_dialog[to] = body
        parts = []
        for d in re.findall(r"<dialog\s.*?</dialog>", body, re.S):
            attr = dict(re.findall(r'([\w-]+)="([^"]*)"', d.split(">", 1)[0]))
            state = (re.search(r"<state[^>]*>([^<]*)</state>", d) or [None, "?"])[1]
            rend = re.findall(r'pname="\+sip\.rendering"\s+pvalue="([^"]*)"', d)
            remote = (re.search(r"<remote>.*?<identity[^>]*>([^<]*)</identity>", d, re.S) or [None, ""])[1]
            parts.append(f"{attr.get('id', '?')} {state} call-id={attr.get('call-id', '')} "
                         f"local-tag={attr.get('local-tag', '')} remote-tag={attr.get('remote-tag', '')} "
                         f"rendering={'/'.join(rend) or '-'} remote={remote}")
        print(f"sharedline: dialog-NOTIFY an {to}: " + ("; ".join(parts) or "keine Dialoge"), flush=True)

    def _reply(self, req: str, addr, status: str, extra: list[str] | None = None) -> None:
        keep = [l for l in req.split("\r\n") if re.match(r"(Via|From|Call-ID|CSeq)\s*:", l, re.I)]
        to = _header(req, "To")
        if ";tag=" not in to:
            to += f";tag={_rnd(8)}"
        lines = [f"SIP/2.0 {status}"] + keep + [f"To: {to}"] + (extra or []) + ["Content-Length: 0", "", ""]
        self.sock.sendto("\r\n".join(lines).encode(), addr)

    def _app_register(self, msg: str, addr) -> None:
        """REGISTER einer App über Flexisip: Name (Anmeldename) und Passwort des Mobilgeräts prüfen.
        Das Gerät muss zur Nummer im To gehören."""
        m = re.search(r"sip:([^@;>]+)@", _header(msg, "To"))
        number = m.group(1) if m else ""
        auth = _header(msg, "Authorization")
        now = time.time()
        for n, t in list(self.nonces.items()):
            if now - t > 600:
                del self.nonces[n]
        ok, known = False, True
        if auth:
            p = digest_params(auth)
            acc = self.credentials(p.get("username", "")) or {}
            known = bool(acc) and _tail(acc.get("number", "")) == _tail(number)
            if known and p.get("nonce") in self.nonces and p.get("realm") == self.realm:
                want = digest_response(p, "REGISTER", p["username"], acc["password"], p.get("uri", ""),
                                       p.get("cnonce", ""), p.get("nc", ""))
                ok = want == p.get("response")
        if not ok:
            if not known:
                self._reply(msg, addr, "403 Forbidden",
                            ["Warning: 399 cme-web \"Gerät in cme-web nicht angelegt oder andere Nummer\""])
                return
            nonce = _rnd(24)
            self.nonces[nonce] = now
            self._reply(msg, addr, "401 Unauthorized",
                        [f'WWW-Authenticate: Digest realm="{self.realm}", nonce="{nonce}", algorithm=MD5, qop="auth"'])
            return
        try:
            expires = min(int(_header(msg, "Expires") or 600), APP_MAX_EXPIRES)
        except ValueError:
            expires = 600
        contacts = _headers(msg, "Contact")
        extra = [f"Contact: {c}" + ("" if "expires=" in c.lower() or c == "*" else f";expires={expires}") for c in contacts]
        extra.append(f"Expires: {expires}")
        if expires:
            self.app_regs[number] = now
        self._reply(msg, addr, "200 OK", extra)

    # ---------- Anmeldung der Mobilgeräte-Pools an CME ----------

    def _register(self, r: Reg, expires: int) -> str:
        auth = ""
        for _ in range(2):
            r.cseq += 1
            fhost, _, fport = self.flexisip.partition(":")
            lines = [
                f"REGISTER sip:{self.cme} SIP/2.0",
                f"Via: SIP/2.0/UDP {self.host}:{self.port};branch=z9hG4bK{_rnd()};rport",
                f"From: <sip:{r.number}@{self.cme}>;tag={r.tag}",
                f"To: <sip:{r.number}@{self.cme}>",
                f"Call-ID: {r.call_id}",
                f"CSeq: {r.cseq} REGISTER",
                "Max-Forwards: 70",
                f"Supported: {CISCO_SUPPORTED}",
                f'Contact: <sip:{r.number}@{fhost}:{fport or 5062};transport=udp>;'
                f'+u.sip!devicename.ccm.cisco.com="{r.device}";+u.sip!model.ccm.cisco.com="503"',
                f"Expires: {expires}",
                "User-Agent: Cisco-CSF",
            ]
            if auth:
                lines.append(f"Authorization: {auth}")
            ev, box = threading.Event(), []
            self.waiting[(r.call_id, r.cseq)] = (ev, box)
            try:
                for _ in range(3):
                    self._send("\r\n".join(lines + ["Content-Length: 0", "", ""]))
                    if ev.wait(2):
                        break
            finally:
                self.waiting.pop((r.call_id, r.cseq), None)
            if not box:
                return "Zeitüberschreitung"
            resp = box[0]
            code = resp.split()[1]
            if code in ("401", "407") and not auth and r.password:
                ch = _header(resp, "WWW-Authenticate") or _header(resp, "Proxy-Authenticate")
                auth = authorization(ch, "REGISTER", f"sip:{self.cme}", r.username, r.password)
                continue
            if code.startswith("2"):
                return ""
            if "another client" in resp:
                # alte Anmeldung mit anderer Call-ID gilt noch: beim nächsten Versuch neu beginnen
                r.call_id, r.tag = _rnd(16), _rnd(9)
            return resp.splitlines()[0][8:]
        return "Anmeldung abgelehnt"

    def sync_regs(self, mobile: list[dict]) -> None:
        """mobile: [{number, device, username, password}] - Pools MOB<Nummer> an CME anmelden."""
        if not self.flexisip:
            return
        now = time.time()
        want = {m["device"]: m for m in mobile}
        with self.lock:
            for dev, r in list(self.regs.items()):
                if dev not in want:
                    self._register(r, 0)
                    del self.regs[dev]
            for dev, m in want.items():
                r = self.regs.get(dev)
                if r is None or (r.username, r.password) != (m["username"], m["password"]):
                    r = self.regs[dev] = Reg(m["number"], dev, m["username"], m["password"])
                if now >= r.next:
                    err = self._register(r, EXPIRES)
                    r.error, r.state = err, ("angemeldet" if not err else "Fehler")
                    if not err and not r.since:
                        r.since = now
                    r.next = now + (EXPIRES * 0.8 if not err else 60)

    def reg_status(self) -> list[dict]:
        with self.lock:
            return [{"number": r.number, "device": r.device, "state": r.state, "error": r.error,
                     "since": int(r.since), "apps": int(self.app_regs.get(r.number, 0))} for r in self.regs.values()]

    def _subscribe(self, s: Sub, expires: int) -> str:
        s.cseq += 1
        to = f"<sip:{s.number}@{self.cme}>" + (f";tag={s.to_tag}" if s.to_tag else "")
        target = s.target or f"sip:{s.number}@{self.cme}"
        msg = "\r\n".join([
            f"SUBSCRIBE {target} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.host}:{self.port};branch=z9hG4bK{_rnd()};rport",
            f"From: <sip:{s.number}@{s.ip}>;tag={s.tag}",
            f"To: {to}",
            f"Call-ID: {s.call_id}",
            f"CSeq: {s.cseq} SUBSCRIBE",
            "Max-Forwards: 70",
            "Event: dialog",
            "Accept: application/dialog-info+xml",
            f"Expires: {expires}",
            f"Contact: <sip:{s.number}@{self.host}:{self.port};transport=udp>",
            "Allow: ACK,BYE,CANCEL,INVITE,NOTIFY,OPTIONS,REFER,REGISTER,UPDATE,SUBSCRIBE",
            "User-Agent: cme-web-shared-line",
            "Content-Length: 0", "", ""])
        ev, box = threading.Event(), []
        self.waiting[(s.call_id, s.cseq)] = (ev, box)
        try:
            for _ in range(3):
                self._send(msg)
                if ev.wait(2):
                    break
        finally:
            self.waiting.pop((s.call_id, s.cseq), None)
        if not box:
            return "Zeitüberschreitung"
        resp = box[0]
        code = resp.split()[1]
        if code.startswith("2"):
            m = re.search(r";\s*tag=([^;>\s]+)", _header(resp, "To"))
            if m:
                s.to_tag = m.group(1)
            c = re.search(r"<([^>]+)>", _header(resp, "Contact"))
            if c:
                s.target = c.group(1)
            return ""
        return resp.splitlines()[0][8:]

    def start(self, s: Sub) -> None:
        err = self._subscribe(s, EXPIRES)
        if err and s.to_tag:
            # Dialog verloren (z. B. CME neu gestartet): neu beginnen
            s.call_id, s.tag, s.to_tag, s.target = _rnd(16), _rnd(9), "", ""
            err = self._subscribe(s, EXPIRES)
        s.error = err
        s.state = "aktiv" if not err else "Fehler"
        if not err and not s.since:
            s.since = time.time()
        s.next = time.time() + (EXPIRES * 0.8 if not err else 60)

    def stop(self, s: Sub) -> None:
        if s.to_tag:
            self._subscribe(s, 0)

    # ---------- Abgleich ----------

    def sync(self, want: list[dict], users: dict[str, set[str]]) -> None:
        now = time.time()
        want_keys = {}
        for w in want:
            k = (w["number"], w["ip"])
            ours = k in self.subs
            if not ours and w["ip"] in users.get(w["number"], set()):
                self.missing.pop(k, None)       # abonniert selbst
                continue
            if not ours:
                self.missing[k] = self.missing.get(k, 0) + 1
                if self.missing[k] < MISSING_ROUNDS:
                    continue
            want_keys[k] = w
        with self.lock:
            for k, s in list(self.subs.items()):
                if k not in want_keys:
                    self.stop(s)
                    del self.subs[k]
            for k, w in want_keys.items():
                s = self.subs.get(k)
                if s is None:
                    s = self.subs[k] = Sub(w["number"], w["ip"], w["pool"])
                if now >= s.next:
                    self.start(s)

    def status(self) -> list[dict]:
        with self.lock:
            return [{"number": s.number, "ip": s.ip, "pool": s.pool, "state": s.state, "error": s.error,
                     "since": int(s.since)} for s in self.subs.values()]

    def stop_all(self) -> None:
        with self.lock:
            for s in self.subs.values():
                self.stop(s)
            self.subs.clear()
            for r in self.regs.values():
                self._register(r, 0)
            self.regs.clear()


def register(app, router, demo: bool, cfgdir: Path) -> None:
    from cme_web import parse_pool_brief, parse_voice_register_config

    store = cfgdir / "cme-sharedline.json"
    cme = os.environ.get("CME_HOST", "").strip()
    host = os.environ.get("SHARED_LINE_HOST", "").strip() or os.environ.get("VM_HOST_IP", "").strip()
    port = int(os.environ.get("SHARED_LINE_PORT", "5074") or 5074)
    flexisip = f"{os.environ.get('VM_HOST_IP', '').strip()}:{os.environ.get('SIP_GW_PORT', '5062') or 5062}" \
        if os.environ.get("VM_HOST_IP", "").strip() else ""
    realm = os.environ.get("SIP_DOMAIN", "").strip() or "cme-web"
    info = {"agent": None, "error": "", "last": 0.0, "candidates": []}

    def mobile_cfg() -> dict:
        return app.config.get("PHONEAPP_MOBILE") or {}

    def credentials(name: str) -> dict:
        """Mobilgerät (z.B. MOBIphone) -> {number, password}"""
        mc = mobile_cfg()
        return mc["account"](name) if mc and "account" in mc else {}

    def mobile_pools(dns: dict, pools: dict) -> list[dict]:
        """Pools MOB<Nummer> mit Zugangsdaten -> an CME anzumelden"""
        mc = mobile_cfg()
        if not mc:
            return []
        known = mc["pools"]()
        out = []
        for p in pools.values():
            dev = p.get("device_id", "").upper()
            if not dev.startswith(mc["prefix"]) or not p.get("numbers"):
                continue
            dn = dns.get(p["numbers"][0]["dn"]) or {}
            c = mc["credentials"](dev, known)
            if dn.get("number") and c:
                out.append({"number": dn["number"], "device": dev, "username": c["username"], "password": c["password"]})
        return out

    def settings() -> dict:
        s = {"enabled": True}
        try:
            s.update(json.loads(store.read_text()))
        except (FileNotFoundError, ValueError):
            pass
        return s

    def check() -> None:
        dns, pools = parse_voice_register_config(router.show("show running-config | section voice register"))
        brief = parse_pool_brief(router.show("show voice register pool all brief"))
        users = parse_shared_users(router.show("show shared-line details"))
        want = wanted(dns, pools, brief, users)
        info["candidates"] = want
        agent = info["agent"]
        if agent is None:
            return
        agent.sync_regs(mobile_pools(dns, pools))
        agent.sync(want if settings()["enabled"] else [], users)

    def loop() -> None:
        while True:
            try:
                if info["agent"] is None and cme and host:
                    info["agent"] = Agent(cme, host, port, flexisip, credentials, realm)
                check()
                info["error"] = ""
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)[:300]
            info["last"] = time.time()
            time.sleep(CHECK_EVERY)

    @app.get("/api/sharedline")
    def sharedline_status():
        a = info["agent"]
        missing = "" if (cme and host) else "CME_HOST und VM_HOST_IP (oder SHARED_LINE_HOST) müssen gesetzt sein."
        return jsonify(ok=True, enabled=settings()["enabled"], host=host, port=port, running=a is not None,
                       error=missing or info["error"], last=int(info["last"]),
                       subs=a.status() if a else [], candidates=info["candidates"],
                       mobile=a.reg_status() if a else [], flexisip=flexisip)

    @app.post("/api/sharedline")
    def sharedline_set():
        d = request.get_json(force=True) or {}
        s = settings()
        s["enabled"] = bool(d.get("enabled"))
        store.write_text(json.dumps(s))
        if not s["enabled"] and info["agent"]:
            threading.Thread(target=info["agent"].stop_all, daemon=True).start()
        else:
            threading.Thread(target=check, daemon=True).start()
        return jsonify(ok=True)

    if not demo:
        threading.Thread(target=loop, daemon=True, name="sharedline").start()
