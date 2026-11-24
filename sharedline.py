"""Shared Line für Geräte, die die Leitung nicht selbst abonnieren (z. B. Gigaset-DECT-Basis).

CME klingelt eine Shared Line (voice register dn … shared-line) nur an den Geräten an, die die Leitung per
SUBSCRIBE Event: dialog abonniert haben ("show shared-line details"). Den Teilnehmer führt CME unter dem
From-URI des Abos und sucht seinen Dial-Peer über den Host darin – das Abo muss also nicht vom Gerät selbst
kommen, es muss nur dessen IP im From tragen. Die INVITEs schickt CME dann direkt an das Gerät.

Cisco-Telefone und CME Phone abonnieren selbst. Für jedes andere angemeldete Gerät auf einer Shared Line, das
CME nicht als Teilnehmer führt, hält dieser Dienst stellvertretend das Abo: SUBSCRIBE mit der Geräte-IP im
From, NOTIFYs bestätigen, rechtzeitig erneuern, beim Abmelden des Geräts beenden.

Braucht einen UDP-Port, den CME erreicht (NOTIFYs): SHARED_LINE_PORT (Standard 5074) auf dem Docker-Host
(VM_HOST_IP). Oberfläche: static/sharedline.js (Tab Telefone).
"""
from __future__ import annotations

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
CHECK_EVERY = 60          # so oft wird die Router-Konfiguration verglichen
MISSING_ROUNDS = 2        # erst abonnieren, wenn das Gerät so oft hintereinander als Teilnehmer fehlt
# Diese Geräte abonnieren selbst
SELF_SUBSCRIBING = re.compile(r"^(\d{4}|Jabber|CiscoMobile|CIPC|CME-Softphone)", re.I)


def _rnd(n: int = 10) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _header(msg: str, name: str) -> str:
    m = re.search(rf"^{name}\s*:\s*(.*)$", msg, re.I | re.M)
    return m.group(1).strip() if m else ""


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


class Agent:
    def __init__(self, cme: str, host: str, port: int):
        self.cme, self.host, self.port = cme, host, port
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
            elif msg.strip():
                # NOTIFY (Event: dialog), OPTIONS u. a. bestätigen – ohne die Bestätigung des ersten NOTIFY
                # nimmt CME das Gerät nicht in die Shared Line auf
                self._reply_ok(msg, addr)

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


def register(app, router, demo: bool, cfgdir: Path) -> None:
    from cme_web import parse_pool_brief, parse_voice_register_config

    store = cfgdir / "cme-sharedline.json"
    cme = os.environ.get("CME_HOST", "").strip()
    host = os.environ.get("SHARED_LINE_HOST", "").strip() or os.environ.get("VM_HOST_IP", "").strip()
    port = int(os.environ.get("SHARED_LINE_PORT", "5074") or 5074)
    info = {"agent": None, "error": "", "last": 0.0, "candidates": []}

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
        agent.sync(want if settings()["enabled"] else [], users)

    def loop() -> None:
        while True:
            try:
                if info["agent"] is None and cme and host:
                    info["agent"] = Agent(cme, host, port)
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
                       subs=a.status() if a else [], candidates=info["candidates"])

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
