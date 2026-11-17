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
CME_SECRET. Anmeldung an der Weboberflaeche per OIDC (Authentik, Pocket-ID, ...)
und/oder lokalem Benutzer CME_WEB_USER / CME_WEB_PASS - siehe auth.py.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import re
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import media

BASE = Path(__file__).resolve().parent

# "" = Fremdgeraet (z.B. Gigaset): kein "type" in der Konfiguration
PHONE_TYPES = ["", "6901", "8811", "8841", "8845", "8851", "8861", "8865", "8945", "ATA190", "7821", "7841", "7861",
               "Jabber-CSF-Client", "Jabber-iPhone", "Jabber-Android"]
# Softclients werden in CME ueber einen Geraetenamen (id device-id-name) erkannt, nicht ueber die MAC
SOFT_CLIENT_PREFIX = "Jabber-"
CODECS = ["g711alaw", "g711ulaw", "g722-64", "g729r8"]
# Codecs fuer Codec-Listen (voice class codec); was der Router kennt, haengt von der IOS-Version ab
LIST_CODECS = ["g711alaw", "g711ulaw", "g722-64", "g729r8", "g729br8", "ilbc", "opus"]
# Arten von Sammelgruppen (voice hunt-group)
HUNT_TYPES = ["parallel", "sequential", "peer", "longest-idle"]

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
    "show running-config | section voice class codec",
    "show running-config | section voice hunt-group",
    "show voice hunt-group",
    "show running-config | section sip-ua",
    "show running-config | include ^tftp-server",
    "show version | include uptime|IOS",
]

# --------------------------------------------------------------------------
# Eingaben pruefen (verhindert, dass ueber Formulare beliebige Befehle
# in die Konfiguration gelangen)
# --------------------------------------------------------------------------

RE_NUMBER = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_TEXT = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{0,40}$")
RE_CRED = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")
RE_TYPE = re.compile(r"^[A-Za-z0-9\-]{2,24}$")
RE_DEVICE = re.compile(r"^[A-Za-z0-9_\-]{3,50}$")


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
        ip_re = r"\d+\.\d+\.\d+\.\d+"
        m = re.match(rf"^\s*(\d+)\s+({mac_re}|{ip_re}|[A-Za-z][\w-]+)\s+(?:({ip_re})\s+)?(\d+)\s+(\d+)\s+(\S+)\s+(\S+)", line)
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
                cur = dns.setdefault(tag, {"tag": tag, "number": "", "name": "", "label": "",
                                           "shared": False, "max_calls": "", "mobility": False,
                                           "snr": None, "snr_raw": ""})
            else:
                cur = pools.setdefault(tag, {"tag": tag, "mac": "", "id_ip": "", "id_mask": "", "device_id": "",
                                             "type": "", "description": "",
                                             "username": "", "codec": "", "codec_list": "",
                                             "numbers": []})
            cur["_kind"] = kind
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        s = line.strip()
        if cur["_kind"] == "dn":
            if s == "shared-line" or s.startswith("shared-line "):
                cur["shared"] = True
                mc = re.search(r"max-calls (\d+)", s)
                cur["max_calls"] = mc.group(1) if mc else ""
                continue
            if s == "mobility":
                cur["mobility"] = True
                continue
            if s.startswith("snr "):
                parse_snr_line(cur, s)
                continue
            for key in ("number", "name", "label"):
                if s.startswith(key + " "):
                    cur[key] = s[len(key) + 1:].strip()
        else:
            if s.startswith("id mac "):
                cur["mac"] = s.split()[2].upper()
            elif s.startswith("id device-id-name "):
                cur["device_id"] = s.split()[2]
            elif s.startswith("id ip ") or s.startswith("id network "):
                parts = s.split()
                cur["id_ip"] = parts[2]
                cur["id_mask"] = parts[4] if len(parts) > 4 and parts[3] == "mask" else ""
            elif s.startswith("type "):
                cur["type"] = s.split(None, 1)[1]
            elif s.startswith("description "):
                cur["description"] = s.split(None, 1)[1]
            elif s.startswith("username "):
                cur["username"] = s.split()[1]
            elif s.startswith("codec "):
                cur["codec"] = s.split()[1]
            elif s.startswith("voice-class codec "):
                cur["codec_list"] = s.split()[2]
            else:
                mm = re.match(r"number (\d+) dn (\d+)", s)
                if mm:
                    cur["numbers"].append({"ln": mm.group(1), "dn": mm.group(2)})
    for d in list(dns.values()) + list(pools.values()):
        d.pop("_kind", None)
    return dns, pools


def parse_snr_line(dn: dict, s: str) -> None:
    """Single Number Reach in voice register dn:
    snr <nummer> delay <s> timeout <s> [cfwd-noan <nummer>] / snr ring-stop /
    snr calling-number local / snr answer-too-soon <s>"""
    snr = dn["snr"] or {"number": "", "delay": "", "timeout": "", "cfwd_noan": "",
                        "ring_stop": False, "calling_local": False, "answer_too_soon": ""}
    m = re.match(r"^snr (\+?[0-9*#]+) delay (\d+) timeout (\d+)(?: cfwd-noan (\S+))?", s)
    if m:
        snr.update(number=m.group(1), delay=m.group(2), timeout=m.group(3), cfwd_noan=m.group(4) or "")
        dn["snr_raw"] = s
    elif s == "snr ring-stop":
        snr["ring_stop"] = True
    elif s.startswith("snr calling-number local"):
        snr["calling_local"] = True
    elif s.startswith("snr answer-too-soon "):
        snr["answer_too_soon"] = s.split()[2]
    else:
        return
    dn["snr"] = snr


def parse_codec_classes(out: str) -> dict[str, dict]:
    """show running-config | section voice class codec -> {tag: {tag, codecs: [...]}}"""
    lists: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^voice class codec\s+(\d+)\s*$", line)
        if m:
            cur = lists.setdefault(m.group(1), {"tag": m.group(1), "prefs": {}})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        mm = re.match(r"^\s+codec preference (\d+) (\S+)", line)
        if cur is not None and mm:
            cur["prefs"][int(mm.group(1))] = mm.group(2)
    for c in lists.values():
        c["codecs"] = [c["prefs"][k] for k in sorted(c["prefs"])]
        del c["prefs"]
    return lists


def parse_hunt_groups(out: str) -> dict[str, dict]:
    """show running-config | section voice hunt-group -> {tag: {...}}"""
    groups: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^voice hunt-group\s+(\d+)\s+(\S+)", line)
        if m:
            cur = groups.setdefault(m.group(1), {"tag": m.group(1), "type": m.group(2).lower(),
                                                 "pilot": "", "secondary": "", "list": [],
                                                 "timeout": "", "final": "", "name": "", "hops": ""})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("pilot "):
            parts = s.split()
            cur["pilot"] = parts[1]
            if len(parts) > 3 and parts[2] == "secondary":
                cur["secondary"] = parts[3]
        elif s.startswith("list "):
            cur["list"] = [x for x in s[5:].replace(" ", "").split(",") if x]
        elif s.startswith("timeout "):
            cur["timeout"] = s.split(None, 1)[1].replace(" ", "")
        elif s.startswith("final "):
            cur["final"] = s.split()[1]
        elif s.startswith("name "):
            cur["name"] = s[5:].strip()
        elif s.startswith("hops "):
            cur["hops"] = s.split()[1]
    return groups


def parse_dialpeer_codecs(out: str) -> dict[str, list[str]]:
    """show running-config | section dial-peer voice -> {codec-list-tag: [dial-peer-tags]}"""
    used: dict[str, list[str]] = {}
    peer = None
    for line in out.splitlines():
        m = re.match(r"^dial-peer voice (\d+)", line)
        if m:
            peer = m.group(1)
            continue
        if not line.startswith(" "):
            peer = None
            continue
        mm = re.match(r"^\s+voice-class codec (\d+)", line)
        if peer and mm:
            used.setdefault(mm.group(1), []).append(peer)
    return used


# CME meldet viele Fehler ohne "%", z.B. "Failed: CME mode only accept mac address ..."
# oder "Error: The password you have entered is invalid."
ERROR_PREFIXES = ("%", "failed", "error")


def config_errors(out: str) -> list[str]:
    errs = []
    lines = out.splitlines()
    for i, l in enumerate(lines):
        t = l.strip()
        if t.lower().startswith(ERROR_PREFIXES):
            errs.append(t)
            # Erlaeuterungen direkt darunter mitnehmen (z.B. Passwortregeln)
            for nxt in lines[i + 1:i + 6]:
                n = nxt.strip()
                if not n or re.match(r"^[\w.-]+(\([\w-]+\))?#", n) or n.lower().startswith(ERROR_PREFIXES):
                    break
                errs.append("  " + n)
    return errs


def password_ok(pw: str) -> bool:
    """Passwortregel von CME: 6-15 Zeichen, nur Buchstaben/Ziffern, je mind. eine Ziffer,
    ein Gross- und ein Kleinbuchstabe."""
    return (re.fullmatch(r"[A-Za-z0-9]{6,15}", pw) is not None and re.search(r"\d", pw) is not None
            and re.search(r"[A-Z]", pw) is not None and re.search(r"[a-z]", pw) is not None)


def new_password() -> str:
    import string
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
        if password_ok(pw):
            return pw


PW_RULE = ("SIP-Passwort: 6-15 Buchstaben/Ziffern ohne Sonderzeichen, mindestens eine Ziffer, "
           "ein Grossbuchstabe und ein Kleinbuchstabe.")


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

    SINGLE = ("id mac", "id ip", "id device-id-name", "type", "description", "voice-class codec", "codec", "username",
              "number", "name", "label", "shared-line", "pilot", "list", "timeout", "final", "hops",
              "mobility", "snr answer-too-soon", "snr calling-number", "snr ring-stop")

    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = [
            "voice register global", " mode cme", " source-address 192.168.1.4 port 5060",
            " max-dn 40", " max-pool 20", " load 8851 sip88xx.14-4-1-0301-6", " create profile",
            "voice register dn  1", " number 101", " name Thomas", " label Thomas Buero",
            "voice register dn  2", " number 102", " name Kueche", " label Kueche",
            "voice register dn  3", " number 103", " name Flur",
            "voice register dn  4", " number 100", " name Zentrale", " label Zentrale",
            " shared-line max-calls 4",
            "voice register dn  6", " number 105", " name Thomas mobil", " mobility",
            " snr 017012345678 delay 5 timeout 20 cfwd-noan 101", " snr ring-stop", " snr calling-number local",
            "voice register pool  1", " id mac 70DB.98A1.0C11", " type 8851", " number 1 dn 1",
            " number 2 dn 4", " description Buero Thomas", " username 101 password x",
            " codec g711alaw", " voice-class codec 2",
            "voice register pool  2", " id mac 00EB.D5C3.4F02", " type 8865", " number 1 dn 2",
            " number 2 dn 4",
            " description Kueche", " username 102 password x", " codec g711alaw",
            "voice register pool  3", " id mac 6C41.0E22.9A03", " type 6901", " number 1 dn 3",
            " description Flur", " username 103 password x", " codec g711alaw",
            "voice register dn  5", " number 110", " name Mobilteil", " label Mobilteil",
            "voice register pool  4", " id ip 192.168.1.60 mask 255.255.255.255", " number 1 dn 5",
            " description Gigaset N510", " username gigaset password x", " codec g711alaw",
            "voice class codec 1", " codec preference 1 g711alaw", " codec preference 2 g711ulaw",
            "voice class codec 2", " codec preference 1 g722-64", " codec preference 2 g711alaw",
            " codec preference 3 g711ulaw",
            "voice hunt-group 1 parallel", " pilot 199", " list 101,102", " timeout 20", " name Alle",
        ]
        self.dialpeers = ("dial-peer voice 100 voip\n description Trunk raus\n destination-pattern 0T\n"
                          " session protocol sipv2\n session target sip-server\n voice-class codec 1\n"
                          "dial-peer voice 200 voip\n description Telefone\n destination-pattern 1..\n")
        self.registered = {"1": "192.168.1.31", "2": "192.168.1.32", "4": "192.168.1.60"}
        media.demo_seed(self)     # self.files, self.tftp

    def show(self, cmd: str) -> str:
        time.sleep(0.2)
        if cmd.startswith("show running-config | section voice register"):
            return "\n".join(l for l in self._blocks("voice register"))
        if cmd.startswith("show running-config | section voice class codec"):
            return "\n".join(l for l in self._blocks("voice class codec"))
        if cmd.startswith("show running-config | section voice hunt-group"):
            return "\n".join(l for l in self._blocks("voice hunt-group"))
        if cmd.startswith("show running-config | section dial-peer"):
            return self.dialpeers
        if cmd.startswith("show running-config | include ^tftp-server"):
            return "\n".join(self.tftp)
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
                        ident = p["mac"] or p["id_ip"]
                        out.append(f"{tag:<4} {ident:<14} {ip:<15} {n['ln']:<2} {n['dn']:<3} {num:<20} {state}")
                    else:
                        out.append(f"{'':<4} {'':<14} {'':<15} {n['ln']:<2} {n['dn']:<3} {num:<20} {state}")
            return "\n".join(out)
        if cmd.startswith("show "):
            return f"(Demo) Keine Beispielausgabe fuer: {cmd}"
        return "% Invalid input detected at '^' marker."

    def _blocks(self, prefix: str):
        take = False
        for l in self.cfg:
            if not l.startswith(" "):
                take = l.startswith(prefix)
            if take:
                yield l

    def configure(self, lines: list[str]) -> str:
        time.sleep(0.3)
        with self.lock:
            block = None
            for line in lines:
                if line.startswith("tftp-server "):
                    if line not in self.tftp:
                        self.tftp.append(line)
                    block = None
                    continue
                if line.startswith("no tftp-server "):
                    if line[3:] in self.tftp:
                        self.tftp.remove(line[3:])
                    block = None
                    continue
                m = re.match(r"^no (voice register (?:dn|pool)|voice class codec|voice hunt-group)\s+(\d+)$",
                             line.strip())
                if m:
                    if m.group(1) == "voice hunt-group":
                        head = next((l for l in self.cfg
                                     if re.match(rf"^voice hunt-group {m.group(2)} ", l)), None)
                    else:
                        head = (f"{m.group(1)}  {m.group(2)}" if m.group(1).startswith("voice register")
                                else f"{m.group(1)} {m.group(2)}")
                    if head in self.cfg:
                        i = self.cfg.index(head)
                        j = i + 1
                        while j < len(self.cfg) and self.cfg[j].startswith(" "):
                            j += 1
                        del self.cfg[i:j]
                    block = None
                    continue
                if not line.startswith(" "):
                    m = re.match(r"^voice register (dn|pool)\s+(\d+)$", line.strip())
                    mc = re.match(r"^voice class codec\s+(\d+)$", line.strip())
                    mh = re.match(r"^voice hunt-group\s+(\d+)\s+(\S+)$", line.strip())
                    if mh:
                        old = next((l for l in self.cfg if re.match(rf"^voice hunt-group {mh.group(1)} ", l)), None)
                        block = f"voice hunt-group {mh.group(1)} {mh.group(2)}"
                        if old is None:
                            self.cfg.append(block)
                        elif old != block:
                            return line + "\n% A hunt-group with this tag and a different type already exists"
                        continue
                    if m or mc:
                        block = (f"voice register {m.group(1)}  {m.group(2)}" if m
                                 else f"voice class codec {mc.group(1)}")
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
                    key = next((k for k in self.SINGLE if s == k or s.startswith(k + " ")), None)
                    if key == "number" and " dn " in s:      # Pool-Leitung: "number 1 dn 3"
                        key = " ".join(s.split()[:2])
                    if s.startswith("codec preference "):      # "codec preference 2 g711alaw"
                        key = " ".join(s.split()[:3])
                    snr_main = re.match(r"^snr \+?[0-9*#]", s) is not None
                    if key or snr_main:
                        for j in range(start, end):
                            t = self.cfg[j].strip()
                            if (re.match(r"^snr \+?[0-9*#]", t) if snr_main
                                    else (t == key or t.startswith(key + " "))):
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

    from auth import setup_auth
    app.config["AUTH"] = setup_auth(app, BASE, web_user, web_pass)

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
        codec_lists = parse_codec_classes(safe("show running-config | section voice class codec"))
        dp_codecs = parse_dialpeer_codecs(safe("show running-config | section dial-peer voice"))
        hunts = parse_hunt_groups(safe("show running-config | section voice hunt-group"))

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
        hunt_member: dict[str, list[str]] = {}
        for h in hunts.values():
            for num in h["list"]:
                hunt_member.setdefault(num, []).append(h["tag"])
        numbers = [{**d, "pools": used.get(t, []), "huntgroups": hunt_member.get(d["number"], [])}
                   for t, d in sorted(dns.items(), key=lambda kv: int(kv[0]))]
        by_number = {n["number"]: n for n in numbers}
        huntgroups = []
        for t, h in sorted(hunts.items(), key=lambda kv: int(kv[0])):
            members = [{"number": num, "dn": by_number.get(num, {}).get("tag", ""),
                        "name": by_number.get(num, {}).get("name", ""),
                        "pools": by_number.get(num, {}).get("pools", [])} for num in h["list"]]
            huntgroups.append({**h, "members": members})
        clists = [{**c, "pools": [p["tag"] for p in phones if p.get("codec_list") == t],
                   "dialpeers": dp_codecs.get(t, [])}
                  for t, c in sorted(codec_lists.items(), key=lambda kv: int(kv[0]))]
        return {"hostname": hostname, "trunks": reg, "dialpeers": peers, "phones": phones,
                "numbers": numbers, "codec_lists": clists, "huntgroups": huntgroups,
                "errors": errors, "ts": time.time()}

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
        return {"ok": not errs, "error": "\n".join(errs) if errs else None, "config": lines,
                "output": out}

    def output_tail(out: str, n: int = 25) -> str:
        rows = [l for l in (out or "").splitlines() if l.strip()]
        return "\n".join(rows[-n:])

    # Hintergrundbilder und Klingeltoene (media.py)
    media.register(app, router, apply, isinstance(router, DemoRouter))

    @app.get("/")
    def index():
        return send_from_directory(BASE / "static", "index.html")

    @app.get("/api/meta")
    def meta():
        return jsonify(phone_types=PHONE_TYPES, codecs=CODECS, list_codecs=LIST_CODECS,
                       hunt_types=HUNT_TYPES,
                       raw_commands=RAW_COMMANDS,
                       demo=isinstance(router, DemoRouter))

    @app.get("/api/status")
    def status():
        return jsonify(get_status(force=request.args.get("force") == "1"))

    def number_input(data: dict) -> dict:
        number = check(data.get("number"), RE_NUMBER, "Rufnummer")
        name = check(data.get("name"), RE_TEXT, "Name", required=False)
        label = check(data.get("label"), RE_TEXT, "Beschriftung", required=False)
        shared = bool(data.get("shared"))
        max_calls = str(data.get("max_calls") or "").strip()
        if max_calls and (not max_calls.isdigit() or not 1 <= int(max_calls) <= 50):
            raise InputError("Max. Anrufe muss eine Zahl zwischen 1 und 50 sein.")
        return {"number": number, "name": name, "label": label, "shared": shared,
                "max_calls": max_calls if shared else "", "snr": snr_input(data.get("snr"), number)}

    def snr_input(d, own_number: str) -> dict | None:
        """Single Number Reach: parallel bzw. verzoegert ein externes Telefon (Handy) anrufen."""
        if not isinstance(d, dict) or not d.get("enabled"):
            return None
        target = check(d.get("number"), RE_NUMBER, "SNR-Zielnummer")
        if target == own_number:
            raise InputError("Die SNR-Zielnummer darf nicht die eigene Rufnummer sein.")

        def num(key, lo, hi, default, what):
            v = str(d.get(key) if d.get(key) not in (None, "") else default).strip()
            if not v.isdigit() or not lo <= int(v) <= hi:
                raise InputError(f"{what} muss zwischen {lo} und {hi} Sekunden liegen.")
            return str(int(v))
        delay = num("delay", 0, 30, 0, "Verzoegerung")
        timeout = num("timeout", 5, 60, 20, "Klingeldauer am Handy")
        ats = str(d.get("answer_too_soon") or "").strip()
        if ats and (not ats.isdigit() or not 1 <= int(ats) <= 5):
            raise InputError("'Zu frueh angenommen' muss zwischen 1 und 5 Sekunden liegen.")
        return {"number": target, "delay": delay, "timeout": timeout,
                "cfwd_noan": check(d.get("cfwd_noan"), RE_NUMBER, "Weiterleitung nach SNR", required=False),
                "ring_stop": bool(d.get("ring_stop")), "calling_local": bool(d.get("calling_local")),
                "answer_too_soon": ats}

    def snr_main_line(s: dict) -> str:
        line = f" snr {s['number']} delay {s['delay']} timeout {s['timeout']}"
        return line + (f" cfwd-noan {s['cfwd_noan']}" if s["cfwd_noan"] else "")

    def snr_lines(new: dict | None, cur: dict | None) -> list[str]:
        """Befehle fuer SNR; cur = bisherige DN (oder None beim Anlegen)."""
        old = (cur or {}).get("snr")
        old_mob = bool((cur or {}).get("mobility"))
        out = []
        if new is None:
            if old:
                if old["ring_stop"]:
                    out.append(" no snr ring-stop")
                if old["calling_local"]:
                    out.append(" no snr calling-number local")
                if old["answer_too_soon"]:
                    out.append(f" no snr answer-too-soon {old['answer_too_soon']}")
                if cur.get("snr_raw"):
                    out.append(f" no {cur['snr_raw']}")
            if old_mob:
                out.append(" no mobility")
            return out
        if not old_mob:
            out.append(" mobility")
        old = old or {"number": "", "delay": "", "timeout": "", "cfwd_noan": "",
                      "ring_stop": False, "calling_local": False, "answer_too_soon": ""}
        if any(new[k] != old[k] for k in ("number", "delay", "timeout", "cfwd_noan")):
            if old["cfwd_noan"] and not new["cfwd_noan"] and cur and cur.get("snr_raw"):
                out.append(f" no {cur['snr_raw']}")      # cfwd-noan entfernen
            out.append(snr_main_line(new))
        if new["ring_stop"] != old["ring_stop"]:
            out.append(" snr ring-stop" if new["ring_stop"] else " no snr ring-stop")
        if new["calling_local"] != old["calling_local"]:
            out.append(" snr calling-number local" if new["calling_local"] else " no snr calling-number local")
        if new["answer_too_soon"] != old["answer_too_soon"]:
            out.append(f" snr answer-too-soon {new['answer_too_soon']}" if new["answer_too_soon"]
                       else f" no snr answer-too-soon {old['answer_too_soon']}")
        return out

    def verify_snr(res: dict, tag: str, v: dict) -> None:
        if not res["ok"]:
            return
        dn = next((n for n in get_status(force=True)["numbers"] if n["tag"] == tag), None)
        if dn is None:
            return
        want, have = v["snr"], dn.get("snr")
        problem = None
        if want and (not have or have["number"] != want["number"] or not dn.get("mobility")):
            problem = f"Single Number Reach fuer DN {tag} wurde vom Router nicht uebernommen."
        elif not want and (dn.get("mobility") or (have and have["number"])):
            problem = f"Single Number Reach fuer DN {tag} ist danach noch aktiv."
        if problem:
            res.update(ok=False, error=f"{problem} Ausgabe des Routers:\n\n{output_tail(res['output'], 40)}")

    def shared_line_cmd(max_calls: str) -> str:
        return f" shared-line max-calls {max_calls}" if max_calls else " shared-line"

    @app.post("/api/numbers")
    def add_number():
        data = request.get_json(force=True) or {}
        v = number_input(data)
        st = get_status(force=True)
        if any(n["number"] == v["number"] for n in st["numbers"]):
            raise InputError(f"Rufnummer {v['number']} gibt es schon.")
        check_not_pilot(st, v["number"])
        tag = str(max([int(n["tag"]) for n in st["numbers"]] + [0]) + 1)
        lines = [f"voice register dn {tag}", f" number {v['number']}"]
        if v["name"]:
            lines.append(f" name {v['name']}")
        if v["label"]:
            lines.append(f" label {v['label']}")
        if v["shared"]:
            lines.append(shared_line_cmd(v["max_calls"]))
        lines += snr_lines(v["snr"], None)
        res = apply(lines)
        res["tag"] = tag
        if res["ok"] and not any(n["tag"] == tag for n in get_status(force=True)["numbers"]):
            res.update(ok=False, error=(
                f"Der Router hat voice register dn {tag} nicht übernommen. "
                f"Ausgabe des Routers:\n\n{output_tail(res['output'])}"))
        verify_snr(res, tag, v)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/numbers/<tag>")
    def edit_number(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Rufnummer")
        data = request.get_json(force=True) or {}
        v = number_input(data)
        st = get_status(force=True)
        cur = next((n for n in st["numbers"] if n["tag"] == tag), None)
        if not cur:
            raise InputError(f"Rufnummer mit DN {tag} gibt es nicht.")
        if any(n["number"] == v["number"] and n["tag"] != tag for n in st["numbers"]):
            raise InputError(f"Rufnummer {v['number']} gibt es schon.")
        if v["number"] != cur["number"]:
            check_not_pilot(st, v["number"])

        lines = [f"voice register dn {tag}"]
        if v["number"] != cur["number"]:
            lines.append(f" number {v['number']}")
        for key in ("name", "label"):
            if v[key] != cur[key]:
                lines.append(f" {key} {v[key]}" if v[key] else f" no {key}")
        if v["shared"] and (not cur["shared"] or v["max_calls"] != cur["max_calls"]):
            # Direkt neu setzen; ein "no shared-line" vorher wuerde der Router bei
            # einer DN auf mehreren Telefonen ablehnen.
            lines.append(shared_line_cmd(v["max_calls"]))
        elif not v["shared"] and cur["shared"]:
            if len(cur["pools"]) > 1:
                raise InputError(
                    "Die Nummer liegt auf mehreren Telefonen (Pool " + ", ".join(cur["pools"]) +
                    "). Erst dort entfernen, dann Shared Line ausschalten.")
            lines.append(" no shared-line")
        lines += snr_lines(v["snr"], cur)

        if len(lines) == 1:
            return jsonify(ok=True, error=None, config=[], tag=tag, unchanged=True)

        lines += ["voice register global", " create profile"]
        if v["number"] != cur["number"]:
            # Sammelgruppen fuehren Rufnummern, nicht DN-Tags: Listen mitziehen
            for h in st["huntgroups"]:
                if cur["number"] in h["list"]:
                    new_list = [v["number"] if x == cur["number"] else x for x in h["list"]]
                    lines += [f"voice hunt-group {h['tag']} {h['type']}", f" list {','.join(new_list)}"]
        if data.get("restart", True):
            for p in cur["pools"]:
                lines += [f"voice register pool {p}", " restart"]
        res = apply(lines)
        res.update(tag=tag, restarted=cur["pools"] if data.get("restart", True) else [])
        verify_snr(res, tag, v)
        return jsonify(res), (200 if res["ok"] else 400)

    def phone_input(data: dict, st: dict, own_tag: str | None) -> dict:
        """Prueft die Formulardaten fuer Anlegen und Bearbeiten."""
        ptype = check(data.get("type"), RE_TYPE, "Telefontyp", required=False)
        mac = ip = device_id = ""
        mask = "255.255.255.255"
        if ptype.startswith(SOFT_CLIENT_PREFIX):
            # Jabber & Co.: Erkennung ueber den Geraetenamen (z.B. CSFTHOMAS)
            id_kind = "device"
            device_id = check(data.get("device_id"), RE_DEVICE, "Geraetename").upper()
        else:
            id_kind = data.get("id_kind") or "mac"
            if id_kind != "mac":
                raise InputError("CME erkennt Telefone nur an der MAC-Adresse, auch Fremdgeraete. "
                                 "Bei Gigaset die MAC der Basis eintragen.")
            mac = norm_mac(data.get("mac"))
        desc = check(data.get("description"), RE_TEXT, "Beschreibung", required=False)
        codec = data.get("codec") or "g711alaw"
        codec_list = ""
        if codec.startswith("list:"):
            codec_list = codec[5:]
            if not any(c["tag"] == codec_list for c in st.get("codec_lists", [])):
                raise InputError(f"Codec-Liste {codec_list} gibt es nicht.")
            codec = ""
        elif codec not in CODECS:
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
        for d in dn_tags:
            others = [p for p in known[d]["pools"] if p != own_tag]
            if others and not known[d]["shared"]:
                raise InputError(
                    f"Rufnummer {known[d]['number']} ist schon Pool {', '.join(others)} zugeordnet. "
                    "Fuer mehrere Telefone die Nummer zuerst als Shared Line markieren.")
        if mac:
            other = next((p for p in st["phones"] if p["mac"].upper() == mac and p["tag"] != own_tag), None)
            if other:
                raise InputError(f"MAC {mac} ist schon Pool {other['tag']} zugeordnet.")
        if ip:
            other = next((p for p in st["phones"] if p.get("id_ip") == ip and p["tag"] != own_tag), None)
            if other:
                raise InputError(f"IP {ip} ist schon Pool {other['tag']} zugeordnet.")
        if device_id:
            other = next((p for p in st["phones"]
                          if p.get("device_id", "").upper() == device_id and p["tag"] != own_tag), None)
            if other:
                raise InputError(f"Geraetename {device_id} ist schon Pool {other['tag']} zugeordnet.")
        return {"id_kind": id_kind, "mac": mac, "ip": ip, "mask": mask, "device_id": device_id, "type": ptype,
                "description": desc, "codec": codec, "codec_list": codec_list,
                "dns": dn_tags, "known": known}

    def codec_line(v: dict) -> str:
        return f" voice-class codec {v['codec_list']}" if v["codec_list"] else f" codec {v['codec']}"

    def id_line(v: dict) -> str:
        if v["id_kind"] == "device":
            return f" id device-id-name {v['device_id']}"
        return f" id mac {v['mac']}" if v["id_kind"] == "mac" else f" id ip {v['ip']} mask {v['mask']}"

    def verify_pool(res: dict, tag: str, v: dict) -> None:
        """Liest die Konfiguration neu und prueft, ob der Router den Pool uebernommen hat."""
        if not res["ok"]:
            return
        pool = next((p for p in get_status(force=True)["phones"] if p["tag"] == tag), None)
        problem = None
        if pool is None:
            problem = f"Der Router hat voice register pool {tag} nicht übernommen."
        elif v["id_kind"] == "ip" and pool.get("id_ip") != v["ip"]:
            problem = f"In Pool {tag} fehlt danach die IP-Kennung {v['ip']}."
        elif v["id_kind"] == "mac" and pool.get("mac") != v["mac"]:
            problem = f"In Pool {tag} fehlt danach die MAC {v['mac']}."
        elif v["id_kind"] == "device" and pool.get("device_id", "").upper() != v["device_id"]:
            problem = f"In Pool {tag} fehlt danach der Geraetename {v['device_id']}."
        elif sorted(l["dn"] for l in pool["lines"]) != sorted(v["dns"]):
            problem = f"In Pool {tag} fehlen danach Leitungen."
        if problem:
            res.update(ok=False, error=(f"{problem} Der Router hat die Befehle nicht übernommen. "
                                        f"Ausgabe des Routers:\n\n{output_tail(res['output'], 40)}"))

    @app.post("/api/phones")
    def add_phone():
        data = request.get_json(force=True) or {}
        st = get_status(force=True)
        v = phone_input(data, st, None)
        username = check(data.get("username") or v["known"][v["dns"][0]]["number"].lstrip("+"),
                         RE_CRED, "Benutzername")
        password = (data.get("password") or "").strip() or new_password()
        if not password_ok(password):
            raise InputError(PW_RULE)
        tag = str(max([int(p["tag"]) for p in st["phones"]] + [0]) + 1)

        lines = [f"voice register pool {tag}", id_line(v)]
        if v["type"]:
            lines.append(f" type {v['type']}")
        lines += [f" number {i} dn {d}" for i, d in enumerate(v["dns"], 1)]
        lines += [" dtmf-relay rtp-nte", codec_line(v), f" username {username} password {password}"]
        if v["description"]:
            lines.append(f" description {v['description']}")
        lines += ["voice register global", " create profile"]
        res = apply(lines)
        res.update(tag=tag, username=username, password=password)
        verify_pool(res, tag, v)
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
        cur_kind = "device" if cur.get("device_id") else ("ip" if cur.get("id_ip") else "mac")
        changed_id = (v["id_kind"] != cur_kind
                      or (cur_kind == "mac" and v["mac"] != cur["mac"].upper())
                      or (cur_kind == "device" and v["device_id"] != cur["device_id"].upper())
                      or (cur_kind == "ip" and (v["ip"], v["mask"]) != (cur["id_ip"], cur.get("id_mask") or v["mask"])))
        if changed_id:
            if cur.get("device_id"):
                lines.append(f" no id device-id-name {cur['device_id']}")
            elif cur.get("id_ip"):
                lines.append(f" no id ip {cur['id_ip']} mask {cur.get('id_mask') or '255.255.255.255'}")
            elif cur["mac"]:
                lines.append(f" no id mac {cur['mac']}")
            lines.append(id_line(v))
        if v["type"] != cur["type"]:
            lines.append(f" type {v['type']}" if v["type"] else f" no type {cur['type']}")
        if v["codec_list"]:
            if v["codec_list"] != cur.get("codec_list"):
                lines.append(codec_line(v))
        elif cur.get("codec_list") or v["codec"] != cur.get("codec"):
            if cur.get("codec_list"):
                lines.append(" no voice-class codec")
            lines.append(codec_line(v))
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
            if not password_ok(password):
                raise InputError(PW_RULE)
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
        verify_pool(res, tag, v)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/phones/<tag>")
    def delete_phone(tag):
        if not tag.isdigit():
            raise InputError("Ungueltiger Pool")
        st = get_status(force=True)
        if not any(p["tag"] == tag for p in st["phones"]):
            raise InputError(f"Pool {tag} gibt es nicht.")
        res = apply([f"no voice register pool {tag}", "voice register global", " create profile"])
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/numbers/<tag>")
    def delete_number(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Rufnummer")
        st = get_status(force=True)
        cur = next((n for n in st["numbers"] if n["tag"] == tag), None)
        if not cur:
            raise InputError(f"Rufnummer mit DN {tag} gibt es nicht.")
        if cur["huntgroups"]:
            raise InputError(f"Rufnummer {cur['number']} ist Mitglied in Sammelgruppe "
                             f"{', '.join(cur['huntgroups'])}. Zuerst dort entfernen.")
        detach = request.args.get("detach") == "1"
        if cur["pools"] and not detach:
            raise InputError(f"Rufnummer {cur['number']} liegt noch auf Pool {', '.join(cur['pools'])}.")
        lines = []
        # Erst von den Telefonen entfernen, dann die DN loeschen
        for p in st["phones"]:
            for l in p["lines"]:
                if l["dn"] == tag:
                    lines += [f"voice register pool {p['tag']}", f" no number {l['ln']} dn {tag}"]
        lines += [f"no voice register dn {tag}", "voice register global", " create profile"]
        for p in cur["pools"]:
            lines += [f"voice register pool {p}", " restart"]
        res = apply(lines)
        res.update(tag=tag, detached=cur["pools"])
        return jsonify(res), (200 if res["ok"] else 400)

    def codec_list_input(data: dict) -> list[str]:
        codecs = [str(c).strip() for c in (data.get("codecs") or []) if str(c).strip()]
        if not codecs:
            raise InputError("Mindestens einen Codec auswaehlen.")
        if len(codecs) > 10:
            raise InputError("Hoechstens 10 Codecs.")
        for c in codecs:
            if c not in LIST_CODECS:
                raise InputError(f"Unbekannter Codec: {c}")
        if len(set(codecs)) != len(codecs):
            raise InputError("Ein Codec ist doppelt ausgewaehlt.")
        return codecs

    def affected_restarts(clist: dict, restart: bool) -> list[str]:
        out = ["voice register global", " create profile"] if clist["pools"] else []
        if restart:
            for p in clist["pools"]:
                out += [f"voice register pool {p}", " restart"]
        return out

    @app.post("/api/codeclists")
    def add_codec_list():
        data = request.get_json(force=True) or {}
        codecs = codec_list_input(data)
        st = get_status(force=True)
        used = {int(c["tag"]) for c in st["codec_lists"]}
        tag = str(next(i for i in range(1, 10000) if i not in used))
        lines = [f"voice class codec {tag}"]
        lines += [f" codec preference {i} {c}" for i, c in enumerate(codecs, 1)]
        res = apply(lines)
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/codeclists/<tag>")
    def edit_codec_list(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Codec-Liste")
        data = request.get_json(force=True) or {}
        codecs = codec_list_input(data)
        st = get_status(force=True)
        cur = next((c for c in st["codec_lists"] if c["tag"] == tag), None)
        if not cur:
            raise InputError(f"Codec-Liste {tag} gibt es nicht.")
        if codecs == cur["codecs"]:
            return jsonify(ok=True, error=None, config=[], tag=tag, unchanged=True)
        # Erst alle alten Praeferenzen entfernen, sonst meldet IOS beim Umsortieren
        # "Codec schon konfiguriert"
        lines = [f"voice class codec {tag}"]
        lines += [f" no codec preference {i}" for i in range(1, len(cur["codecs"]) + 1)]
        lines += [f" codec preference {i} {c}" for i, c in enumerate(codecs, 1)]
        lines += affected_restarts(cur, data.get("restart", True))
        res = apply(lines)
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/codeclists/<tag>")
    def delete_codec_list(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Codec-Liste")
        st = get_status(force=True)
        cur = next((c for c in st["codec_lists"] if c["tag"] == tag), None)
        if not cur:
            raise InputError(f"Codec-Liste {tag} gibt es nicht.")
        users = [f"Pool {p}" for p in cur["pools"]] + [f"Dial-Peer {d}" for d in cur["dialpeers"]]
        if users:
            raise InputError(f"Codec-Liste {tag} wird noch verwendet: {', '.join(users)}.")
        res = apply([f"no voice class codec {tag}"])
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    # ---------------- Sammelgruppen (voice hunt-group) ----------------

    def check_not_pilot(st: dict, number: str, own_tag: str | None = None) -> None:
        for h in st.get("huntgroups", []):
            if h["tag"] != own_tag and number in (h["pilot"], h["secondary"]):
                raise InputError(f"{number} ist schon Pilotnummer der Sammelgruppe {h['tag']}.")

    def hunt_input(data: dict, st: dict, own_tag: str | None) -> dict:
        htype = (data.get("type") or "").strip()
        if htype not in HUNT_TYPES:
            raise InputError("Ungueltige Art der Sammelgruppe")
        pilot = check(data.get("pilot"), RE_NUMBER, "Pilotnummer")
        if any(n["number"] == pilot for n in st["numbers"]):
            raise InputError(f"{pilot} ist schon eine Rufnummer (DN). Die Pilotnummer muss eine "
                             "eigene Nummer sein; die Rufnummer zuerst umbenennen.")
        check_not_pilot(st, pilot, own_tag)
        members = [str(m).strip() for m in (data.get("members") or []) if str(m).strip()]
        if not members:
            raise InputError("Mindestens ein Mitglied auswaehlen.")
        if len(members) > 20:
            raise InputError("Hoechstens 20 Mitglieder.")
        if len(set(members)) != len(members):
            raise InputError("Ein Mitglied ist doppelt ausgewaehlt.")
        known = {n["number"] for n in st["numbers"]}
        for m in members:
            if m not in known:
                raise InputError(f"Rufnummer {m} gibt es nicht.")
        timeout = str(data.get("timeout") or "").strip()
        if timeout and (not timeout.isdigit() or not 3 <= int(timeout) <= 600):
            raise InputError("Klingeldauer muss zwischen 3 und 600 Sekunden liegen.")
        final = check(data.get("final"), RE_NUMBER, "Weiterleitung", required=False)
        if final and final == pilot:
            raise InputError("Die Weiterleitung darf nicht auf die eigene Pilotnummer zeigen.")
        name = check(data.get("name"), RE_TEXT, "Name", required=False)
        return {"type": htype, "pilot": pilot, "members": members, "timeout": timeout,
                "final": final, "name": name}

    def hunt_lines(tag: str, v: dict, cur: dict | None) -> list[str]:
        """Befehle fuer eine Sammelgruppe; bei cur=None alles, sonst nur Aenderungen."""
        lines = [f"voice hunt-group {tag} {v['type']}"]
        if cur is None or v["pilot"] != cur["pilot"]:
            lines.append(f" pilot {v['pilot']}")
        if cur is None or v["members"] != cur["list"]:
            lines.append(f" list {','.join(v['members'])}")
        for key in ("timeout", "final", "name"):
            old = cur[key] if cur else ""
            if v[key] != old:
                lines.append(f" {key} {v[key]}" if v[key] else f" no {key}")
        return lines

    def verify_hunt(res: dict, tag: str, v: dict) -> None:
        if not res["ok"]:
            return
        h = next((x for x in get_status(force=True)["huntgroups"] if x["tag"] == tag), None)
        problem = None
        if h is None:
            problem = f"Der Router hat die Sammelgruppe {tag} nicht übernommen."
        elif h["pilot"] != v["pilot"]:
            problem = f"In Sammelgruppe {tag} fehlt danach die Pilotnummer {v['pilot']}."
        elif h["list"] != v["members"]:
            problem = f"In Sammelgruppe {tag} stimmen die Mitglieder danach nicht."
        if problem:
            res.update(ok=False, error=(f"{problem} Ausgabe des Routers:\n\n"
                                        f"{output_tail(res['output'], 40)}"))

    @app.post("/api/huntgroups")
    def add_huntgroup():
        data = request.get_json(force=True) or {}
        st = get_status(force=True)
        v = hunt_input(data, st, None)
        used = {int(h["tag"]) for h in st["huntgroups"]}
        tag = str(next(i for i in range(1, 101) if i not in used))
        res = apply(hunt_lines(tag, v, None))
        res["tag"] = tag
        verify_hunt(res, tag, v)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/huntgroups/<tag>")
    def edit_huntgroup(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Sammelgruppe")
        data = request.get_json(force=True) or {}
        st = get_status(force=True)
        cur = next((h for h in st["huntgroups"] if h["tag"] == tag), None)
        if not cur:
            raise InputError(f"Sammelgruppe {tag} gibt es nicht.")
        v = hunt_input(data, st, tag)
        if v["type"] != cur["type"]:
            # Die Art laesst sich in IOS nicht aendern: Gruppe neu anlegen
            lines = [f"no voice hunt-group {tag}"] + hunt_lines(tag, v, None)
        else:
            lines = hunt_lines(tag, v, cur)
            if len(lines) == 1:
                return jsonify(ok=True, error=None, config=[], tag=tag, unchanged=True)
        res = apply(lines)
        res["tag"] = tag
        verify_hunt(res, tag, v)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/huntgroups/<tag>")
    def delete_huntgroup(tag):
        if not tag.isdigit():
            raise InputError("Ungueltige Sammelgruppe")
        st = get_status(force=True)
        if not any(h["tag"] == tag for h in st["huntgroups"]):
            raise InputError(f"Sammelgruppe {tag} gibt es nicht.")
        res = apply([f"no voice hunt-group {tag}"])
        res["tag"] = tag
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
    auth = app.config["AUTH"]
    if auth["enabled"]:
        print("Anmeldung: " + ", ".join(auth["methods"]), flush=True)
    elif args.listen not in ("127.0.0.1", "localhost", "::1"):
        print("Hinweis: Die Oberflaeche ist im Netz erreichbar, aber ohne Login. "
              "Setze OIDC_PROVIDERS oder CME_WEB_USER/CME_WEB_PASS.", flush=True)
    print(f"Oeffne http://{args.listen}:{args.web_port}", flush=True)
    try:
        from waitress import serve
        serve(app, host=args.listen, port=args.web_port, threads=8)
    except ImportError:
        app.run(host=args.listen, port=args.web_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
