#!/usr/bin/env python3
"""
cme_web.py - Kleine Weboberflaeche fuer Cisco Call Manager Express (SIP)

Zeigt den Status der SIP-Trunks (Registrierung beim Provider, Dial-Peers),
der SIP-Telefone und Rufnummern und kann Rufnummern (voice register dn)
und Telefone (voice register pool) anlegen.

Der Server verbindet sich per SSH (netmiko) mit dem Router. Er lauscht
standardmaessig nur auf 127.0.0.1, ist also nur vom eigenen Rechner aus
erreichbar.

Start:
  .venv/bin/pip install flask netmiko
  .venv/bin/python3 cme_web.py --host 192.168.1.4 --user thomas
  -> http://127.0.0.1:8080

Demo ohne Router:
  .venv/bin/python3 cme_web.py --demo

Optionen auch per Umgebungsvariable: CME_HOST, CME_USER, CME_PASSWORD,
CME_SECRET, CME_WEB_USER / CME_WEB_PASS (Login fuer die Weboberflaeche).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

BASE = Path(__file__).resolve().parent

PHONE_TYPES = ["6901", "8811", "8841", "8845", "8851", "8861", "8865", "ATA190", "7821", "7841", "7861"]
CODECS = ["g711alaw", "g711ulaw", "g729r8"]

# Nur diese show-Befehle duerfen ueber die Oberflaeche roh abgefragt werden
RAW_COMMANDS = [
    "show sip-ua register status",
    "show sip-ua status",
    "show dial-peer voice summary",
    "show voice register pool all brief",
    "show voice register dn summary",
    "show voice register global",
    "show voice register tftp-bind",
    "show sip-ua calls",
    "show call active voice brief",
    "show running-config | section voice register",
    "show running-config | section dial-peer",
    "show running-config | section sip-ua",
    "show version | include uptime|IOS",
]

# --------------------------------------------------------------------------
# Eingaben pruefen (verhindert, dass ueber Formulare beliebige Befehle
# in die Konfiguration gelangen)
# --------------------------------------------------------------------------

RE_NUMBER = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_TEXT = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{0,40}$")
RE_CRED = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")
RE_TYPE = re.compile(r"^[A-Za-z0-9\-]{2,12}$")


class InputError(ValueError):
    pass


def norm_mac(mac: str) -> str:
    hexs = re.sub(r"[^0-9A-Fa-f]", "", mac or "")
    if len(hexs) != 12:
        raise InputError("MAC-Adresse muss 12 Hex-Zeichen haben, z.B. 00AA.11BB.22CC")
    hexs = hexs.upper()
    return f"{hexs[0:4]}.{hexs[4:8]}.{hexs[8:12]}"


def check(value: str, regex: re.Pattern, what: str, required: bool = True) -> str:
    value = (value or "").strip()
    if not value and not required:
        return ""
    if not value or not regex.match(value):
        raise InputError(f"Ungueltige Eingabe bei {what}: {value!r}")
    return value


# --------------------------------------------------------------------------
# Parser fuer die Router-Ausgaben
# --------------------------------------------------------------------------

def parse_register_status(out: str) -> list[dict]:
    """show sip-ua register status"""
    rows = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\S+)\s+(-?\d+)\s+(-?\d+)\s+(yes|no)\b\s*(\S+)?", line)
        if m and not line.startswith("="):
            rows.append({
                "line": m.group(1), "peer": m.group(2), "expires": int(m.group(3)),
                "registered": m.group(4) == "yes", "info": m.group(5) or "",
            })
    return rows


def parse_dial_peers(out: str) -> list[dict]:
    """show dial-peer voice summary"""
    rows = []
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+(voip|pots|sip|mmoip)\s+(up|down)\s+(up|down)\s*(.*)$", line, re.I)
        if m:
            rest = m.group(5).split()
            target = next((t for t in rest if t.startswith(("ipv4:", "dns:", "sip-server", "enum:"))), "")
            keepalive = next((t for t in rest if t.lower() in ("active", "busyout", "up", "down")), "")
            # Muster steht direkt vor der Praeferenz-Zahl, auf die "syst"/"on"/"off" folgt
            pattern = ""
            for i in range(1, len(rest) - 1):
                if rest[i].isdigit() and rest[i + 1].lower() in ("syst", "on", "off", "none"):
                    pattern = rest[i - 1]
                    break
            rows.append({
                "tag": m.group(1), "type": m.group(2).lower(), "admin": m.group(3).lower(),
                "oper": m.group(4).lower(), "pattern": pattern, "target": target,
                "keepalive": keepalive, "raw": line.strip(),
            })
    return rows


def parse_pool_brief(out: str) -> dict[str, dict]:
    """show voice register pool all brief -> {pool: {mac, ip, state, lines:[...]}}"""
    pools: dict[str, dict] = {}
    last = None
    mac_re = r"[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}"
    for line in out.splitlines():
        m = re.match(rf"^\s*(\d+)\s+({mac_re})\s+(?:(\d+\.\d+\.\d+\.\d+)\s+)?(\d+)\s+(\d+)\s+(\S+)\s+(\S+)", line)
        if m:
            last = m.group(1)
            pools[last] = {"mac": m.group(2).upper(), "ip": m.group(3) or "",
                           "state": m.group(7).upper(),
                           "lines": [{"ln": m.group(4), "dn": m.group(5), "number": m.group(6),
                                      "state": m.group(7).upper()}]}
            continue
        m = re.match(r"^\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$", line)
        if m and last:
            pools[last]["lines"].append({"ln": m.group(1), "dn": m.group(2),
                                         "number": m.group(3), "state": m.group(4).upper()})
    return pools


def parse_voice_register_config(out: str) -> tuple[dict, dict]:
    """show running-config | section voice register -> (dns, pools)"""
    dns: dict[str, dict] = {}
    pools: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^voice register (dn|pool)\s+(\d+)\s*$", line)
        if m:
            kind, tag = m.groups()
            if kind == "dn":
                cur = dns.setdefault(tag, {"tag": tag, "number": "", "name": "", "label": ""})
            else:
                cur = pools.setdefault(tag, {"tag": tag, "mac": "", "type": "", "description": "",
                                             "username": "", "codec": "", "numbers": []})
            cur["_kind"] = kind
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        s = line.strip()
        if cur["_kind"] == "dn":
            for key in ("number", "name", "label"):
                if s.startswith(key + " "):
                    cur[key] = s[len(key) + 1:].strip()
        else:
            if s.startswith("id mac "):
                cur["mac"] = s.split()[2].upper()
            elif s.startswith("type "):
                cur["type"] = s.split(None, 1)[1]
            elif s.startswith("description "):
                cur["description"] = s.split(None, 1)[1]
            elif s.startswith("username "):
                cur["username"] = s.split()[1]
            elif s.startswith("codec "):
                cur["codec"] = s.split()[1]
            else:
                mm = re.match(r"number (\d+) dn (\d+)", s)
                if mm:
                    cur["numbers"].append({"ln": mm.group(1), "dn": mm.group(2)})
    for d in list(dns.values()) + list(pools.values()):
        d.pop("_kind", None)
    return dns, pools


def config_errors(out: str) -> list[str]:
    return [l.strip() for l in out.splitlines() if l.strip().startswith("%")]


# --------------------------------------------------------------------------
# Router-Verbindung
# --------------------------------------------------------------------------

class Router:
    def __init__(self, host, user, password, secret="", port=22):
        self.params = dict(device_type="cisco_ios", host=host, port=port, username=user,
                           password=password, secret=secret, fast_cli=False)
        self.conn = None
        self.lock = threading.Lock()

    def _connect(self):
        from netmiko import ConnectHandler
        self.conn = ConnectHandler(**self.params)
        if self.params["secret"]:
            self.conn.enable()
        self.conn.send_command("terminal length 0")

    def _do(self, fn):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None or not self.conn.is_alive():
                        self._connect()
                    return fn(self.conn)
                except Exception:
                    try:
                        if self.conn:
                            self.conn.disconnect()
                    except Exception:
                        pass
                    self.conn = None
                    if attempt == 2:
                        raise

    def show(self, cmd: str) -> str:
        return self._do(lambda c: c.send_command(cmd, read_timeout=60))

    def configure(self, lines: list[str]) -> str:
        return self._do(lambda c: c.send_config_set(lines, read_timeout=120))

    def save(self) -> str:
        return self._do(lambda c: c.save_config())


class DemoRouter:
    """Spielt einen Router mit Beispieldaten nach, zum Ausprobieren der Oberflaeche."""

    SINGLE = ("id mac", "type", "description", "codec", "username", "number", "name", "label")

    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = [
            "voice register global", " mode cme", " source-address 192.168.1.4 port 5060",
            " max-dn 40", " max-pool 20", " load 8851 sip88xx.14-4-1-0301-6", " create profile",
            "voice register dn  1", " number 101", " name Thomas", " label Thomas Buero",
            "voice register dn  2", " number 102", " name Kueche", " label Kueche",
            "voice register dn  3", " number 103", " name Flur",
            "voice register pool  1", " id mac 70DB.98A1.0C11", " type 8851", " number 1 dn 1",
            " number 2 dn 2", " description Buero Thomas", " username 101 password x",
            " codec g711alaw",
            "voice register pool  2", " id mac 00EB.D5C3.4F02", " type 8865", " number 1 dn 2",
            " description Kueche", " username 102 password x", " codec g711alaw",
            "voice register pool  3", " id mac 6C41.0E22.9A03", " type 6901", " number 1 dn 3",
            " description Flur", " username 103 password x", " codec g711alaw",
        ]
        self.registered = {"1": "192.168.1.31", "2": "192.168.1.32"}

    def show(self, cmd: str) -> str:
        time.sleep(0.2)
        if cmd.startswith("show running-config | section voice register"):
            return "\n".join(self.cfg)
        if cmd.startswith("show running-config | include ^hostname"):
            return "hostname cme-demo"
        if cmd == "show sip-ua register status":
            return ("Tenant: 0\n--------------------Registrar-Index  1 ---------------------\n"
                    "Line                             peer       expires(sec) reg survival P-Associated-URI\n"
                    "================================ ========== ============ === ======== ================\n"
                    "4930123456789                    20001      2763         yes normal\n"
                    "4930123456780                    20002      2759         yes normal\n"
                    "4930123456781                    20003      -1           no  normal\n")
        if cmd == "show dial-peer voice summary":
            return ("dial-peer hunt 0\n             AD                                    PRE PASS                OUT\n"
                    "TAG    TYPE  MIN  OPER PREFIX    DEST-PATTERN      FER THRU SESS-TARGET    STAT PORT    KEEPALIVE\n"
                    "100    voip  up   up             0T                  0  syst sip-server            active\n"
                    "101    voip  up   up             110                 0  syst sip-server            active\n"
                    "200    voip  up   up             1..                 0  syst                       \n"
                    "300    voip  up   down           9T                  0  syst ipv4:10.0.0.5:5060    busyout\n")
        if cmd == "show voice register pool all brief":
            _, pools = parse_voice_register_config("\n".join(self.cfg))
            dns, _ = parse_voice_register_config("\n".join(self.cfg))
            out = ["Pool ID             IP Address      Ln DN  Number               State",
                   "==== ============== =============== == === ==================== ============"]
            for tag, p in pools.items():
                ip = self.registered.get(tag, "")
                state = "REGISTERED" if ip else "UNREGISTERED"
                for i, n in enumerate(p["numbers"]):
                    num = dns.get(n["dn"], {}).get("number", "")
                    if i == 0:
                        out.append(f"{tag:<4} {p['mac']:<14} {ip:<15} {n['ln']:<2} {n['dn']:<3} {num:<20} {state}")
                    else:
                        out.append(f"{'':<4} {'':<14} {'':<15} {n['ln']:<2} {n['dn']:<3} {num:<20} {state}")
            return "\n".join(out)
        if cmd.startswith("show "):
            return f"(Demo) Keine Beispielausgabe fuer: {cmd}"
        return "% Invalid input detected at '^' marker."

    def configure(self, lines: list[str]) -> str:
        time.sleep(0.3)
        with self.lock:
            block = None
            for line in lines:
                if not line.startswith(" "):
                    m = re.match(r"^voice register (dn|pool)\s+(\d+)$", line.strip())
                    if m:
                        block = f"voice register {m.group(1)}  {m.group(2)}"
                        if block not in self.cfg:
                            self.cfg.append(block)
                    else:
                        block = None
                    continue
                if block and line.strip() not in ("create profile", "restart", "reset"):
                    start = self.cfg.index(block) + 1
                    end = start
                    while end < len(self.cfg) and self.cfg[end].startswith(" "):
                        end += 1
                    s = line.strip()
                    if s.startswith("no "):
                        target = " " + s[3:]
                        for j in range(start, end):
                            if self.cfg[j] == target or self.cfg[j].startswith(target + " "):
                                del self.cfg[j]
                                break
                        continue
                    key = next((k for k in self.SINGLE if s.startswith(k + " ")), None)
                    if key == "number" and " dn " in s:      # Pool-Leitung: "number 1 dn 3"
                        key = " ".join(s.split()[:2])
                    if key:
                        for j in range(start, end):
                            if self.cfg[j].strip().startswith(key + " "):
                                del self.cfg[j]
                                end -= 1
                                break
                    self.cfg.insert(end, line)
        return "\n".join(lines)

    def save(self) -> str:
        time.sleep(0.3)
        return "[OK]"


# --------------------------------------------------------------------------
# Web-App
# --------------------------------------------------------------------------

def create_app(router, web_user: str = "", web_pass: str = "") -> Flask:
    app = Flask(__name__, static_folder=None)
    state = {"cache": None, "cache_ts": 0.0, "dirty": False}
    cache_lock = threading.Lock()

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.before_request
    def auth():
        if not web_user or request.path == "/healthz":
            return None
        a = request.authorization
        if not a or not (secrets.compare_digest(a.username or "", web_user)
                         and secrets.compare_digest(a.password or "", web_pass)):
            return Response("Login erforderlich", 401, {"WWW-Authenticate": 'Basic realm="CME"'})
        return None

    @app.errorhandler(InputError)
    def on_input_error(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.errorhandler(Exception)
    def on_error(e):
        app.logger.exception("Fehler")
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500

    def collect() -> dict:
        errors = []

        def safe(cmd):
            try:
                out = router.show(cmd)
                if "% Invalid" in out:
                    errors.append(f"{cmd}: vom Router nicht unterstuetzt")
                return out
            except Exception as e:
                errors.append(f"{cmd}: {type(e).__name__}: {e}")
                return ""

        hostname = safe("show running-config | include ^hostname").replace("hostname", "").strip()
        reg = parse_register_status(safe("show sip-ua register status"))
        peers = parse_dial_peers(safe("show dial-peer voice summary"))
        dns, pools = parse_voice_register_config(safe("show running-config | section voice register"))
        brief = parse_pool_brief(safe("show voice register pool all brief"))

        dn_by_tag = {t: d for t, d in dns.items()}
        phones = []
        for tag, p in sorted(pools.items(), key=lambda kv: int(kv[0])):
            b = brief.get(tag, {})
            lines = [{"ln": n["ln"], "dn": n["dn"],
                      "number": dn_by_tag.get(n["dn"], {}).get("number", ""),
                      "name": dn_by_tag.get(n["dn"], {}).get("name", "")} for n in p["numbers"]]
            phones.append({**p, "lines": lines, "ip": b.get("ip", ""),
                           "state": b.get("state", "UNBEKANNT" if not brief else "UNREGISTERED")})
        used = {}
        for p in pools.values():
            for n in p["numbers"]:
                used.setdefault(n["dn"], []).append(p["tag"])
        numbers = [{**d, "pools": used.get(t, [])} for t, d in sorted(dns.items(), key=lambda kv: int(kv[0]))]
        return {"hostname": hostname, "trunks": reg, "dialpeers": peers, "phones": phones,
                "numbers": numbers, "errors": errors, "ts": time.time()}

    def get_status(force=False) -> dict:
        with cache_lock:
            if force or not state["cache"] or time.time() - state["cache_ts"] > 10:
                state["cache"] = collect()
                state["cache_ts"] = time.time()
            return {**state["cache"], "dirty": state["dirty"]}

    def apply(lines: list[str]) -> dict:
        out = router.configure(lines)
        errs = config_errors(out)
        state["dirty"] = True
        state["cache_ts"] = 0
        return {"ok": not errs, "error": "\n".join(errs) if errs else None, "config": lines}

    @app.get("/")
    def index():
        return send_from_directory(BASE / "static", "index.html")

    @app.get("/api/meta")
    def meta():
        return jsonify(phone_types=PHONE_TYPES, codecs=CODECS, raw_commands=RAW_COMMANDS,
                       demo=isinstance(router, DemoRouter))

    @app.get("/api/status")
    def status():
        return jsonify(get_status(force=request.args.get("force") == "1"))

    @app.post("/api/numbers")
    def add_number():
        data = request.get_json(force=True) or {}
        number = check(data.get("number"), RE_NUMBER, "Rufnummer")
        name = check(data.get("name"), RE_TEXT, "Name", required=False)
        label = check(data.get("label"), RE_TEXT, "Beschriftung", required=False)
        st = get_status(force=True)
        if any(n["number"] == number for n in st["numbers"]):
            raise InputError(f"Rufnummer {number} gibt es schon.")
        tag = str(max([int(n["tag"]) for n in st["numbers"]] + [0]) + 1)
        lines = [f"voice register dn {tag}", f" number {number}"]
        if name:
            lines.append(f" name {name}")
        if label:
            lines.append(f" label {label}")
        res = apply(lines)
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    def phone_input(data: dict, st: dict, own_tag: str | None) -> dict:
        """Prueft die Formulardaten fuer Anlegen und Bearbeiten."""
        mac = norm_mac(data.get("mac"))
        ptype = check(data.get("type"), RE_TYPE, "Telefontyp")
        desc = check(data.get("description"), RE_TEXT, "Beschreibung", required=False)
        codec = data.get("codec") or "g711alaw"
        if codec not in CODECS:
            raise InputError("Ungueltiger Codec")
        dn_tags = [str(d) for d in (data.get("dns") or []) if str(d).strip()]
        if not dn_tags:
            raise InputError("Mindestens eine Rufnummer auswaehlen.")
        known = {n["tag"]: n for n in st["numbers"]}
        for d in dn_tags:
            if d not in known:
                raise InputError(f"Rufnummer mit Tag {d} gibt es nicht.")
        if len(set(dn_tags)) != len(dn_tags):
            raise InputError("Eine Rufnummer ist doppelt ausgewaehlt.")
        other = next((p for p in st["phones"] if p["mac"].upper() == mac and p["tag"] != own_tag), None)
        if other:
            raise InputError(f"MAC {mac} ist schon Pool {other['tag']} zugeordnet.")
        return {"mac": mac, "type": ptype, "description": desc, "codec": codec,
                "dns": dn_tags, "known": known}

    @app.post("/api/phones")
    def add_phone():
        data = request.get_json(force=True) or {}
        st = get_status(force=True)
        v = phone_input(data, st, None)
        username = check(data.get("username") or v["known"][v["dns"][0]]["number"].lstrip("+"),
                         RE_CRED, "Benutzername")
        password = check(data.get("password") or secrets.token_hex(6), RE_CRED, "Passwort")
        tag = str(max([int(p["tag"]) for p in st["phones"]] + [0]) + 1)

        lines = [f"voice register pool {tag}", f" id mac {v['mac']}", f" type {v['type']}"]
        lines += [f" number {i} dn {d}" for i, d in enumerate(v["dns"], 1)]
        lines += [" dtmf-relay rtp-nte", f" codec {v['codec']}", f" username {username} password {password}"]
        if v["description"]:
            lines.append(f" description {v['description']}")
        lines += ["voice register global", " create profile"]
        res = apply(lines)
        res.update(tag=tag, username=username, password=password)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/phones/<tag>")
    def edit_phone(tag):
        if not tag.isdigit():
            raise InputError("Ungueltiger Pool")
        data = request.get_json(force=True) or {}
        st = get_status(force=True)
        cur = next((p for p in st["phones"] if p["tag"] == tag), None)
        if not cur:
            raise InputError(f"Pool {tag} gibt es nicht.")
        v = phone_input(data, st, tag)

        lines = [f"voice register pool {tag}"]
        if v["mac"] != cur["mac"].upper():
            if cur["mac"]:
                lines.append(f" no id mac {cur['mac']}")
            lines.append(f" id mac {v['mac']}")
        if v["type"] != cur["type"]:
            lines.append(f" type {v['type']}")
        if v["codec"] != cur.get("codec"):
            lines.append(f" codec {v['codec']}")
        if v["description"] != cur["description"]:
            lines.append(f" description {v['description']}" if v["description"] else " no description")

        old_lines = [(l["ln"], l["dn"]) for l in cur["lines"]]
        new_lines = [(str(i), d) for i, d in enumerate(v["dns"], 1)]
        # Nur geaenderte Leitungen anfassen: erst entfernen, dann neu setzen
        lines += [f" no number {ln} dn {dn}" for ln, dn in old_lines if (ln, dn) not in new_lines]
        lines += [f" number {ln} dn {dn}" for ln, dn in new_lines if (ln, dn) not in old_lines]

        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        if password:
            username = check(username or cur["username"] or v["known"][v["dns"][0]]["number"].lstrip("+"),
                             RE_CRED, "Benutzername")
            password = check(password, RE_CRED, "Passwort")
            lines.append(f" username {username} password {password}")
        elif username and username != cur["username"]:
            raise InputError("Fuer einen neuen SIP-Benutzer bitte auch ein Passwort angeben.")

        if len(lines) == 1:
            return jsonify(ok=True, error=None, config=[], tag=tag, unchanged=True)

        lines += ["voice register global", " create profile"]
        if data.get("restart", True):
            lines += [f"voice register pool {tag}", " restart"]
        res = apply(lines)
        res.update(tag=tag, username=username or cur["username"], password=password or None)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.post("/api/phones/<tag>/restart")
    def restart_phone(tag):
        if not tag.isdigit():
            raise InputError("Ungueltiger Pool")
        out = router.configure([f"voice register pool {tag}", " restart"])
        errs = config_errors(out)
        return jsonify(ok=not errs, error="\n".join(errs) or None)

    @app.post("/api/save")
    def save():
        out = router.save()
        state["dirty"] = False
        return jsonify(ok="[OK]" in out or "OK" in out, output=out)

    @app.get("/api/raw")
    def raw():
        cmd = request.args.get("cmd", "")
        if cmd not in RAW_COMMANDS:
            raise InputError("Befehl nicht erlaubt")
        return jsonify(ok=True, output=router.show(cmd))

    return app


def main():
    ap = argparse.ArgumentParser(description="Weboberflaeche fuer Cisco CME (SIP)")
    ap.add_argument("--host", default=os.environ.get("CME_HOST"))
    ap.add_argument("--user", default=os.environ.get("CME_USER"))
    ap.add_argument("--password", default=os.environ.get("CME_PASSWORD"))
    ap.add_argument("--secret", default=os.environ.get("CME_SECRET", ""))
    ap.add_argument("--port", type=int, default=int(os.environ.get("CME_SSH_PORT", "22")),
                    help="SSH-Port des Routers")
    ap.add_argument("--listen", default=os.environ.get("CME_LISTEN", "127.0.0.1"),
                    help="Adresse fuer die Weboberflaeche (Standard 127.0.0.1)")
    ap.add_argument("--web-port", type=int, default=int(os.environ.get("CME_WEB_PORT", "8080")))
    ap.add_argument("--demo", action="store_true",
                    default=os.environ.get("CME_DEMO", "").lower() in ("1", "true", "yes"),
                    help="Ohne Router mit Beispieldaten starten")
    args = ap.parse_args()

    web_user = os.environ.get("CME_WEB_USER", "")
    web_pass = os.environ.get("CME_WEB_PASS", "")
    if args.listen not in ("127.0.0.1", "localhost", "::1") and not web_user:
        print("Hinweis: Die Oberflaeche ist im Netz erreichbar, aber ohne Login. "
              "Setze CME_WEB_USER und CME_WEB_PASS.")

    if args.demo:
        router = DemoRouter()
    else:
        if not (args.host and args.user):
            ap.error("--host und --user sind noetig (oder --demo).")
        password = args.password or getpass.getpass(f"Passwort fuer {args.user}@{args.host}: ")
        router = Router(args.host, args.user, password, args.secret, args.port)
        print(f"Teste Verbindung zu {args.host} ...", flush=True)
        try:
            router.show("show clock")
            print("Verbindung ok.", flush=True)
        except Exception as e:
            # Nicht abbrechen: im Container wuerde das sonst eine Neustart-Schleife ausloesen.
            # Die Oberflaeche zeigt den Fehler an und versucht es beim naechsten Laden erneut.
            print(f"Warnung: Router nicht erreichbar ({type(e).__name__}: {e})", flush=True)

    app = create_app(router, web_user, web_pass)
    print(f"Oeffne http://{args.listen}:{args.web_port}", flush=True)
    try:
        from waitress import serve
        serve(app, host=args.listen, port=args.web_port, threads=8)
    except ImportError:
        app.run(host=args.listen, port=args.web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
