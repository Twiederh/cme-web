"""
voicemail.py - Voicemail mit Asterisk (Container cme-voicemail) fuer cme_web

cme-web schreibt die Asterisk-Konfiguration in ein gemeinsames Volume (VM_CONFIG_DIR,
in Asterisk /etc/asterisk/cme), liest die Nachrichten aus dem Spool-Volume (VM_SPOOL_DIR)
und steuert Asterisk ueber AMI (Reload, MWI, Nachrichten verschieben/loeschen).

Am Router richtet es ein:
  sip-ua / mwi-server ipv4:<VM_HOST_IP> ... unsolicited   (Lampe am Telefon)
  voice register global / voicemail <pilot>               (Nachrichten-Taste)
  dial-peer voice <tag> voip -> session target ipv4:<VM_HOST_IP>
  voice register dn <n> / mwi, call-forward b2bua noan|busy <pilot>
  telephony-service / codec g722-64, service phone g722CodecSupport 2 ...  (G.722 an den Telefonen)
"""

from __future__ import annotations

import configparser
import json
import math
import os
import re
import socket
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_NUM = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_PIN = re.compile(r"^[0-9]{4,10}$")
RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
FOLDERS = {"INBOX": "Neu", "Old": "Gehört"}


class VMError(ValueError):
    pass


def vm_key(number: str) -> str:
    """Vergleichsform einer Anrufernummer fuer die Fernabfrage (wie cme-normkey im Asterisk-Dialplan):
    nur Ziffern, ohne fuehrende Nullen, ohne 49 bei deutschen Nummern mit Laendervorwahl.
    001796907106, 01796907106, +491796907106 -> 1796907106"""
    k = re.sub(r"\D", "", number or "").lstrip("0")
    if k.startswith("49") and len(k) >= 12:
        k = k[2:]
    return k


# --------------------------------------------------------------------------
# AMI
# --------------------------------------------------------------------------

class AMI:
    def __init__(self, host: str, port: int, user: str, secret: str):
        self.host, self.port, self.user, self.secret = host, port, user, secret
        self.lock = threading.Lock()

    def _read(self, f) -> dict:
        """Liest eine Antwort (Block mit Response:), ueberspringt Events."""
        while True:
            block: dict = {"_lines": []}
            while True:
                line = f.readline()
                if not line:
                    raise ConnectionError("AMI-Verbindung geschlossen")
                line = line.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if block["_lines"]:
                        break
                    continue
                block["_lines"].append(line)
                if ":" in line:
                    k, v = line.split(":", 1)
                    if k == "Output":
                        block.setdefault("Output", []).append(v.strip())
                    else:
                        block[k] = v.strip()
            if "Response" in block:
                return block

    def run(self, *actions: dict) -> list[dict]:
        with self.lock:
            with socket.create_connection((self.host, self.port), timeout=6) as s:
                f = s.makefile("rwb")
                f.readline()  # Begruessung
                res = []
                for a in [{"Action": "Login", "Username": self.user, "Secret": self.secret}, *actions,
                          {"Action": "Logoff"}]:
                    f.write(("".join(f"{k}: {v}\r\n" for k, v in a.items()) + "\r\n").encode())
                    f.flush()
                    r = self._read(f)
                    if a["Action"] == "Login" and r.get("Response") != "Success":
                        raise PermissionError("AMI-Anmeldung abgelehnt (VM_AMI_SECRET pruefen)")
                    res.append(r)
                return res[1:-1]

    def command(self, cmd: str) -> str:
        r = self.run({"Action": "Command", "Command": cmd})[0]
        return "\n".join(r.get("Output", []))


class DemoAMI:
    """Ohne Asterisk: Verschieben/Loeschen direkt im Spool."""

    def __init__(self, spool: Path):
        self.spool = spool

    def command(self, cmd: str) -> str:
        if cmd == "core show version":
            return "Asterisk 20.6.0 (Demo)"
        return ""

    def run(self, *actions: dict) -> list[dict]:
        out = []
        for a in actions:
            if a["Action"] in ("VoicemailRemove", "VoicemailMove"):
                d = self.spool / a["Context"] / a["Mailbox"] / a["Folder"]
                for txt in sorted(d.glob("msg*.txt")):
                    if read_msg(txt).get("msg_id") == a["ID"]:
                        if a["Action"] == "VoicemailRemove":
                            for f in d.glob(txt.stem + ".*"):
                                f.unlink()
                        else:
                            dest = self.spool / a["Context"] / a["Mailbox"] / a["ToFolder"]
                            dest.mkdir(parents=True, exist_ok=True)
                            n = len(list(dest.glob("msg*.txt")))
                            for f in d.glob(txt.stem + ".*"):
                                f.rename(dest / f"msg{n:04d}{f.suffix}")
                        resequence(d)
            out.append({"Response": "Success"})
        return out


def resequence(folder: Path) -> None:
    msgs = sorted({f.stem for f in folder.glob("msg*.*")})
    for i, stem in enumerate(msgs):
        new = f"msg{i:04d}"
        if stem != new:
            for f in folder.glob(stem + ".*"):
                f.rename(folder / (new + f.suffix))


# --------------------------------------------------------------------------
# Spool lesen
# --------------------------------------------------------------------------

def read_msg(txt: Path) -> dict:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(txt.read_text(encoding="utf-8", errors="replace"))
        return dict(cp["message"]) if cp.has_section("message") else {}
    except Exception:
        return {}


def parse_callerid(cid: str) -> tuple[str, str]:
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<([^>]*)>\s*$', cid or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", (cid or "").strip()


def list_messages(spool: Path, mailbox: str) -> list[dict]:
    out = []
    for folder in FOLDERS:
        d = spool / "default" / mailbox / folder
        if not d.is_dir():
            continue
        for txt in sorted(d.glob("msg*.txt")):
            info = read_msg(txt)
            wav = txt.with_suffix(".wav")
            name, number = parse_callerid(info.get("callerid", ""))
            out.append({"folder": folder, "file": txt.stem, "id": info.get("msg_id", ""),
                        "time": int(info.get("origtime") or 0), "duration": int(info.get("duration") or 0),
                        "caller_name": name, "caller": number, "audio": wav.exists()})
    out.sort(key=lambda m: -m["time"])
    return out


def demo_seed(spool: Path, cfgdir: Path) -> None:
    if (cfgdir / "cme-voicemail.json").exists():
        return
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "cme-voicemail.json").write_text(json.dumps({
        "pilot": "8000", "maxsecs": 180, "maxmsg": 50, "skip_pin": True,
        "mailboxes": {"101": {"pin": "1234", "timeout": 20, "busy": True}}}, indent=2))
    d = spool / "default" / "101" / "INBOX"
    d.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    for i, (cid, age, secs) in enumerate([('"Max Muster" <061871234567>', 3600, 4),
                                          ('<+4917012345678>', 86400, 2)]):
        (d / f"msg{i:04d}.txt").write_text(
            f";\n[message]\norigmailbox=101\ncallerid={cid}\norigtime={now - age}\n"
            f"msg_id={now - age}-0000000{i}\nduration={secs}\n")
        with wave.open(str(d / f"msg{i:04d}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * (440 + 220 * i) * t / 8000)))
                                   for t in range(8000 * secs)))


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------

def register(app, router, apply, demo: bool) -> None:
    cfgdir = Path(os.environ.get("VM_CONFIG_DIR", "/data/asterisk"))
    spool = Path(os.environ.get("VM_SPOOL_DIR", "/data/voicemail"))
    host_ip = os.environ.get("VM_HOST_IP", "")
    cme_ip = os.environ.get("CME_HOST", "")
    secret = os.environ.get("VM_AMI_SECRET", "") or os.environ.get("AMI_SECRET", "")
    if demo:
        tmp = Path(tempfile.gettempdir()) / "cme-web-demo-vm"
        cfgdir, spool = tmp / "config", tmp / "spool"
        host_ip, cme_ip = host_ip or "192.168.1.20", cme_ip or "192.168.1.4"
        demo_seed(spool, cfgdir)
        ami = DemoAMI(spool)
    else:
        ami = AMI(os.environ.get("VM_AMI_HOST", "asterisk"), int(os.environ.get("VM_AMI_PORT", "5038")),
                  os.environ.get("VM_AMI_USER", "cmeweb"), secret) if secret else None
    enabled = ami is not None and bool(host_ip)
    # E-Mail-Versand eingerichtet (SMTP-Zugang im Stack)?
    mail_ready = demo or bool(os.environ.get("VM_SMTP_USER"))

    def settings() -> dict:
        s = {"pilot": "8000", "maxsecs": 180, "maxmsg": 100, "skip_pin": True, "mailboxes": {}}
        try:
            s.update(json.loads((cfgdir / "cme-voicemail.json").read_text()))
        except FileNotFoundError:
            pass
        return s

    def mbid(number: str) -> str:
        return re.sub(r"\D", "", number)

    def named(msgs: list[dict]) -> list[dict]:
        """Fehlende Anrufernamen aus dem Telefonbuch (phonebook.py) ergaenzen."""
        pb = app.config.get("PHONEBOOK")
        if pb:
            for m in msgs:
                if not m["caller_name"] and m["caller"]:
                    try:
                        m["caller_name"] = pb["name"](m["caller"])
                    except Exception:
                        pass
        return msgs

    def write_config(s: dict, names: dict[str, str]) -> bool:
        """Schreibt die Asterisk-Dateien; True, wenn sich die MWI-Ziele geaendert haben."""
        cfgdir.mkdir(parents=True, exist_ok=True)
        boxes = s["mailboxes"]
        try:
            alias = aliases(s, numbers())
        except Exception:  # noqa: BLE001
            alias = {}
        mb_lines, mwi = [], []
        for number, b in sorted(boxes.items()):
            name = re.sub(r"[,|]", " ", names.get(number, "") or number)
            email = b.get("email", "")
            mb_lines.append(f"{mbid(number)} => {b['pin']},{name},{email},,{'attach=yes' if email else ''}")
            ep = f"vm-{mbid(number)}"
            mwi += [f"[{ep}]", "type=aor", f"contact=sip:{number}@{cme_ip}:5060", "",
                    f"[{ep}]", "type=endpoint", "transport=transport-udp", f"aors={ep}",
                    f"mailboxes={mbid(number)}@default", "context=from-cme", "disallow=all", "allow=g722,alaw,ulaw", ""]
        # Mitglieder eines Sammelanschlusses: Lampe am eigenen Telefon fuer das Postfach der Gruppe
        for member, number in sorted(alias.items()):
            ep = f"vm-{mbid(member)}"
            mwi += [f"[{ep}]", "type=aor", f"contact=sip:{member}@{cme_ip}:5060", "",
                    f"[{ep}]", "type=endpoint", "transport=transport-udp", f"aors={ep}",
                    f"mailboxes={mbid(number)}@default", "context=from-cme", "disallow=all", "allow=g722,alaw,ulaw", ""]
        files = {
            "mailboxes.conf": "\n".join(mb_lines) + "\n",
            "pjsip_mwi.conf": "\n".join(mwi) + "\n",
            "vm_general.conf": f"maxmsg={int(s['maxmsg'])}\nmaxsecs={int(s['maxsecs'])}\nminsecs=2\ntz=european\n",
            "globals.conf": f"VM_PILOT={s['pilot']}\nVM_MAIN_OPTS={'s' if s['skip_pin'] else ''}\n" + "".join(
                f"VMR_{vm_key(r)}={mbid(number)}\nVMRP_{vm_key(r)}={0 if b.get('remote_pin') is False else 1}\n"
                for number, b in sorted(boxes.items()) for r in b.get("remote", [])) + "".join(
                f"VMA_{mbid(member)}={mbid(number)}\n" for member, number in sorted(alias.items())),
            "cme-voicemail.json": json.dumps(s, indent=2),
        }
        try:
            mwi_changed = (cfgdir / "pjsip_mwi.conf").read_text() != files["pjsip_mwi.conf"]
        except FileNotFoundError:
            mwi_changed = True
        for name, text in files.items():
            tmp = cfgdir / f".{name}.tmp"
            tmp.write_text(text)
            tmp.replace(cfgdir / name)
        return mwi_changed

    def reload_asterisk(restart: bool = False) -> None:
        if restart:
            # Neue MWI-Ziele uebernimmt Asterisk erst nach einem Neustart (Reload reicht nicht);
            # "when convenient" wartet laufende Gespraeche ab.
            try:
                ami.command("core restart when convenient")
            except Exception:
                pass
            return
        for cmd in ("module reload app_voicemail.so", "dialplan reload"):
            ami.command(cmd)

    # ---------------- Router ----------------

    def cme_state(s: dict) -> dict:
        sipua = router.show("show running-config | section sip-ua")
        glob = router.show("show running-config | section voice register global")
        peers = router.show("show running-config | section dial-peer voice")
        dns = router.show("show running-config | section voice register dn")
        st = {"mwi_server": bool(re.search(rf"mwi-server ipv4:{re.escape(host_ip)}\b.*unsolicited", sipua)),
              "voicemail": bool(re.search(rf"^\s+voicemail {re.escape(s['pilot'])}\s*$", glob, re.M)),
              "dialpeer": "", "dns": {}}
        cur = None
        for line in peers.splitlines():
            m = re.match(r"^dial-peer voice (\d+)", line)
            if m:
                cur = {"tag": m.group(1), "pattern": "", "target": ""}
                continue
            if cur is not None:
                mm = re.match(r"^\s+destination-pattern (\S+)", line)
                if mm:
                    cur["pattern"] = mm.group(1)
                mm = re.match(r"^\s+session target ipv4:([\d.]+)", line)
                if mm:
                    cur["target"] = mm.group(1)
                if cur["pattern"] == s["pilot"] and cur["target"] == host_ip:
                    st["dialpeer"] = cur["tag"]
        dn = None
        for line in dns.splitlines():
            m = re.match(r"^voice register dn\s+(\d+)", line)
            if m:
                dn = st["dns"].setdefault(m.group(1), {"mwi": False, "noan": "", "timeout": "", "busy": ""})
                continue
            if not line.startswith(" "):
                dn = None
            if dn is None:
                continue
            t = line.strip()
            if t == "mwi":
                dn["mwi"] = True
            mm = re.match(r"call-forward b2bua noan (\S+)(?: timeout (\d+))?", t)
            if mm:
                dn["noan"], dn["timeout"] = mm.group(1), mm.group(2) or ""
            mm = re.match(r"call-forward b2bua busy (\S+)", t)
            if mm:
                dn["busy"] = mm.group(1)
        return st

    def used_peer_tags() -> set[int]:
        return {int(t) for t in re.findall(r"^dial-peer voice (\d+)", router.show(
            "show running-config | section dial-peer voice"), re.M)}

    def numbers() -> list[dict]:
        out = router.show("show running-config | section voice register dn")
        res, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^voice register dn\s+(\d+)", line)
            if m:
                cur = {"tag": m.group(1), "number": "", "name": ""}
                res.append(cur)
                continue
            if not line.startswith(" "):
                cur = None
            if cur:
                t = line.strip()
                if t.startswith("number "):
                    cur["number"] = t.split()[1]
                elif t.startswith("name "):
                    cur["name"] = t[5:].strip()
        res = [n for n in res if n["number"]]
        # Sammelanschluesse (voice hunt-group): eigenes Postfach unter der Pilotnummer; die Mitglieder
        # (eigene interne Nummern) leiten auf dieses Postfach um, sofern sie kein eigenes haben
        dn_nums = {n["number"] for n in res}
        for g in hunt_groups().values():
            if g["pilot"] and g["pilot"] not in dn_nums:
                res.append({"tag": f"hg{g['tag']}", "number": g["pilot"], "hunt": g["tag"], "type": g["type"],
                            "name": g["name"] or f"Sammelanschluss {g['tag']}",
                            "members": [m for m in g["list"] if m in dn_nums]})
        return res

    def hunt_groups() -> dict[str, dict]:
        from cme_web import parse_hunt_groups
        try:
            return parse_hunt_groups(router.show("show running-config | section voice hunt-group"))
        except Exception:  # noqa: BLE001
            return {}

    def aliases(s: dict, nums: list[dict]) -> dict[str, str]:
        """Mitgliedsnummer -> Pilotnummer, fuer Sammelanschluesse mit Postfach (Mitglied ohne eigenes)."""
        out = {}
        for n in nums:
            if n.get("hunt") and n["number"] in s["mailboxes"]:
                for m in n["members"]:
                    if m not in s["mailboxes"]:
                        out.setdefault(m, n["number"])
        return out

    def hunt_lines(n: dict, b: dict | None, pilot: str, st: dict) -> list[str]:
        """Router-Zeilen fuer das Postfach eines Sammelanschlusses: Umleitung der Mitglieder und
        final (nach Ablauf der Gruppe) auf die Voicemail."""
        s = settings()
        dn_tags = {x["number"]: x["tag"] for x in numbers() if not x.get("hunt")}
        out = []
        for m in n["members"]:
            if m in s["mailboxes"] or m not in dn_tags:
                continue
            out += dn_lines(dn_tags[m], b, pilot, st["dns"].get(dn_tags[m], {}))
        out += [f"voice hunt-group {n['hunt']} {n['type']}"]
        if b:
            out += [f" final {pilot}", f" timeout {int(b['timeout'])}"]
        return out

    def hunt_state(n: dict, st: dict, pilot: str) -> dict:
        """Umleitungsstatus eines Sammelanschlusses = aller Mitglieder (fuer die Anzeige)."""
        dn_tags = {x["number"]: x["tag"] for x in numbers() if not x.get("hunt")}
        own = settings()["mailboxes"]
        ds = [st["dns"].get(dn_tags[m], {}) for m in n["members"] if m in dn_tags and m not in own]
        if not ds:
            return {}
        ok = lambda k: all(d.get(k) == pilot for d in ds)  # noqa: E731
        return {"mwi": all(d.get("mwi") for d in ds), "noan": pilot if ok("noan") else "",
                "busy": pilot if ok("busy") else "", "timeout": ds[0].get("timeout", "")}

    # ---------------- API ----------------

    @app.errorhandler(VMError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    def need_enabled():
        if not enabled:
            raise VMError("Voicemail ist nicht eingerichtet: VM_HOST_IP und VM_AMI_SECRET fehlen.")

    @app.get("/static/voicemail.js")
    def voicemail_js():
        return send_file(BASE / "static" / "voicemail.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/voicemail")
    def get_vm():
        if not enabled:
            return jsonify(ok=True, enabled=False)
        s = settings()
        status = {"asterisk": False, "version": "", "error": ""}
        try:
            status["version"] = ami.command("core show version").split(" built")[0]
            status["asterisk"] = True
        except Exception as e:
            status["error"] = f"{type(e).__name__}: {e}"
        try:
            cme = cme_state(s)
            nums = numbers()
        except Exception as e:
            cme, nums = {"error": f"{type(e).__name__}: {e}", "dns": {}}, []
        boxes = []
        for n in nums:
            b = s["mailboxes"].get(n["number"])
            dn = hunt_state(n, cme, s["pilot"]) if n.get("hunt") and "dns" in cme and not cme.get("error") \
                else cme.get("dns", {}).get(n["tag"], {})
            msgs = named(list_messages(spool, mbid(n["number"]))) if b else []
            boxes.append({"tag": n["tag"], "number": n["number"], "name": n["name"], "enabled": bool(b),
                          "hunt": n.get("hunt", ""), "members": n.get("members", []),
                          "pin": (b or {}).get("pin", ""), "timeout": (b or {}).get("timeout", 20),
                          "busy": (b or {}).get("busy", True), "email": (b or {}).get("email", ""),
                          "remote": (b or {}).get("remote", []), "remote_pin": (b or {}).get("remote_pin", True),
                          "id": mbid(n["number"]),
                          "cme": dn, "new": sum(m["folder"] == "INBOX" for m in msgs),
                          "old": sum(m["folder"] == "Old" for m in msgs), "messages": msgs})
        return jsonify(ok=True, enabled=True, host_ip=host_ip, mail=mail_ready, settings={k: v for k, v in s.items() if k != "mailboxes"},
                       status=status, cme=cme, mailboxes=boxes)

    @app.put("/api/voicemail/settings")
    def put_settings():
        need_enabled()
        d = request.get_json(force=True) or {}
        s = settings()
        old_pilot = s["pilot"]
        pilot = str(d.get("pilot") or "").strip()
        if not RE_NUM.match(pilot):
            raise VMError("Ungueltige Voicemail-Nummer.")
        try:
            maxsecs, maxmsg = int(d.get("maxsecs") or 180), int(d.get("maxmsg") or 100)
        except ValueError:
            raise VMError("Zahlen erwartet.")
        if not 10 <= maxsecs <= 600 or not 1 <= maxmsg <= 999:
            raise VMError("Laenge 10-600 s, Nachrichten 1-999.")
        if pilot != old_pilot and any(n["number"] == pilot for n in numbers()):
            raise VMError(f"{pilot} ist schon eine Rufnummer.")
        s.update(pilot=pilot, maxsecs=maxsecs, maxmsg=maxmsg, skip_pin=bool(d.get("skip_pin")))
        names = {n["number"]: n["name"] for n in numbers()}
        reload_asterisk(write_config(s, names))
        res = {"ok": True, "error": None, "config": []}
        if pilot != old_pilot:
            # Router auf die neue Nummer umstellen
            res = setup_cme(s, old_pilot)
        return jsonify(res), (200 if res["ok"] else 400)

    def setup_cme(s: dict, old_pilot: str | None = None) -> dict:
        st = cme_state({**s, "pilot": old_pilot or s["pilot"]})
        lines = ["sip-ua", f" mwi-server ipv4:{host_ip} expires 3600 port 5060 transport udp unsolicited",
                 "voice register global", f" voicemail {s['pilot']}"]
        tag = st["dialpeer"] or str(next(t for t in range(8000, 9000) if t not in used_peer_tags()))
        lines += [f"dial-peer voice {tag} voip", " description Voicemail (Asterisk)",
                  f" destination-pattern {s['pilot']}", " session protocol sipv2",
                  f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte", codec_line(), " no vad"]
        # Tastentoene: ohne RTP-Events kommen Tastendruecke nicht bei Asterisk an
        pools, cur, need = router.show("show running-config | section voice register pool"), None, []
        for line in pools.splitlines() + [""]:
            m = re.match(r"^voice register pool\s+(\d+)", line)
            if m or not line.startswith(" "):
                if cur and not cur[1]:
                    need.append(cur[0])
                cur = [m.group(1), False] if m else None
            elif cur and line.split()[:1] == ["dtmf-relay"] and {"rtp-nte", "sip-notify"} <= set(line.split()):
                cur[1] = True
        for p in need:
            lines += [f"voice register pool {p}", " dtmf-relay rtp-nte sip-notify"]
        # G.722: SIP-Telefone der 78xx/88xx-Reihe bieten G.722 erst an, wenn es unter
        # telephony-service freigeschaltet ist (auch ohne SCCP-Telefone)
        ts = router.show("show running-config | section telephony-service")
        g722 = [x for x in (" codec g722-64", " service phone g722CodecSupport 2",
                            " service phone advertiseG722Codec 1") if not re.search(rf"^{re.escape(x)}\s*$", ts, re.M)]
        if g722:
            lines += ["telephony-service", *g722]
            # alle Telefone muessen die neue Konfigurationsdatei laden
            restart = sorted(set(need) | set(re.findall(r"^voice register pool\s+(\d+)", pools, re.M)), key=int)
        else:
            restart = need
        # Umleitungen der Postfaecher auf die (neue) Nummer
        for n in numbers():
            b = s["mailboxes"].get(n["number"])
            if b:
                lines += hunt_lines(n, b, s["pilot"], st) if n.get("hunt") else dn_lines(n["tag"], b, s["pilot"], None)
        lines += ["voice register global", " create profile"]
        for p in restart:   # Telefone laden die neue Einstellung erst nach einem Neustart
            lines += [f"voice register pool {p}", " restart"]
        res = apply(lines)
        res["dtmf_pools"] = need
        res["restarted_pools"] = restart
        return res

    VM_CODEC = 8998   # eigene Codec-Liste fuer Asterisk: G.722 zuerst

    def codec_line() -> str:
        """G.722 bevorzugt: erste Codec-Liste (voice class codec), die mit G.722 beginnt.
        Gibt es keine, wird die Liste 8998 (G.722, G.711 a-law, G.711 u-law) angelegt."""
        out = router.show("show running-config | section voice class codec")
        lists, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^voice class codec\s+(\d+)", line)
            if m:
                cur = lists.setdefault(int(m.group(1)), {})
                continue
            mm = re.match(r"^\s+codec preference (\d+) (\S+)", line)
            if cur is not None and mm:
                cur[int(mm.group(1))] = mm.group(2)
        for t in sorted(lists):
            if lists[t] and lists[t][min(lists[t])].startswith("g722"):
                return f" voice-class codec {t}"
        if VM_CODEC not in lists:
            res = apply([f"voice class codec {VM_CODEC}", " codec preference 1 g722-64",
                         " codec preference 2 g711alaw", " codec preference 3 g711ulaw"])
            if not res.get("ok"):
                return " codec g711alaw"
        return f" voice-class codec {VM_CODEC}"

    def dn_lines(tag: str, b: dict | None, pilot: str, cur: dict | None) -> list[str]:
        out = [f"voice register dn {tag}"]
        if b:
            out += [" mwi", f" call-forward b2bua noan {pilot} timeout {int(b['timeout'])}"]
            out.append(f" call-forward b2bua busy {pilot}" if b["busy"] else " no call-forward b2bua busy")
        else:
            cur = cur or {}
            if cur.get("mwi"):
                out.append(" no mwi")
            if cur.get("noan"):
                out.append(" no call-forward b2bua noan")
            if cur.get("busy"):
                out.append(" no call-forward b2bua busy")
        return out

    @app.post("/api/voicemail/cme")
    def post_cme():
        need_enabled()
        res = setup_cme(settings())
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/voicemail/mailboxes/<tag>")
    def put_mailbox(tag):
        need_enabled()
        if not re.fullmatch(r"(hg)?\d+", tag):
            raise VMError("Ungueltige DN")
        d = request.get_json(force=True) or {}
        nums = numbers()
        n = next((x for x in nums if x["tag"] == tag), None)
        if not n:
            raise VMError(f"DN {tag} gibt es nicht.")
        s = settings()
        if d.get("enabled"):
            pin = str(d.get("pin") or "").strip()
            if not RE_PIN.match(pin):
                raise VMError("PIN: 4-10 Ziffern.")
            try:
                timeout = int(d.get("timeout") or 20)
            except ValueError:
                raise VMError("Klingeldauer muss eine Zahl sein.")
            if not 5 <= timeout <= 120:
                raise VMError("Klingeldauer 5-120 Sekunden.")
            if mbid(n["number"]) in {mbid(k) for k in s["mailboxes"] if k != n["number"]}:
                raise VMError("Ein anderes Postfach hat dieselbe Nummer (ohne +).")
            email = str(d.get("email") or "").strip()
            if email and not RE_EMAIL.match(email):
                raise VMError("Ungueltige E-Mail-Adresse.")
            remote = []
            raw = d.get("remote") or []
            if isinstance(raw, str):
                raw = re.split(r"[,;\n]+", raw)
            own = {mbid(x["number"]) for x in nums}
            taken = {vm_key(r): k for k, mb in s["mailboxes"].items() if k != n["number"] for r in mb.get("remote", [])}
            for r in raw:
                r = re.sub(r"[\s/()-]", "", str(r))
                if not r:
                    continue
                if not RE_NUM.match(r) or len(vm_key(r)) < 6:
                    raise VMError(f"Fernabfrage: {r} ist keine vollständige externe Rufnummer.")
                if mbid(r) in own:
                    raise VMError(f"Fernabfrage: {r} ist eine interne Rufnummer.")
                if vm_key(r) in taken:
                    raise VMError(f"Fernabfrage: {r} gehört schon zum Postfach {taken[vm_key(r)]}.")
                if vm_key(r) not in {vm_key(x) for x in remote}:
                    remote.append(r)
            b = {"pin": pin, "timeout": timeout, "busy": bool(d.get("busy")), "email": email,
                 "remote": remote, "remote_pin": d.get("remote_pin", True) is not False}
            s["mailboxes"][n["number"]] = b
        else:
            b = None
            s["mailboxes"].pop(n["number"], None)
        restart = write_config(s, {x["number"]: x["name"] for x in nums})
        reload_asterisk(restart)
        st = cme_state(s)
        lines = hunt_lines(n, b, s["pilot"], st) if n.get("hunt") else dn_lines(tag, b, s["pilot"], st["dns"].get(tag, {}))
        res = apply(lines + ["voice register global", " create profile"])
        if b and not restart:
            try:
                ami.run({"Action": "VoicemailRefresh", "Context": "default", "Mailbox": mbid(n["number"])})
            except Exception:
                pass
        res["asterisk_restart"] = restart
        return jsonify(res), (200 if res["ok"] else 400)

    def msg_path(box: str, folder: str, file: str) -> Path:
        if not re.fullmatch(r"\d{1,32}", box) or folder not in FOLDERS or not re.fullmatch(r"msg\d{4}", file):
            raise VMError("Ungueltige Nachricht")
        return spool / "default" / box / folder / file

    @app.get("/api/voicemail/audio/<box>/<folder>/<file>")
    def audio(box, folder, file):
        need_enabled()
        p = msg_path(box, folder, file).with_suffix(".wav")
        if not p.exists():
            return jsonify(ok=False, error="Aufnahme nicht gefunden"), 404
        return send_file(p, mimetype="audio/wav", max_age=0,
                         as_attachment=request.args.get("download") == "1",
                         download_name=f"voicemail-{box}-{file}.wav")

    def msg_action(box: str, folder: str, file: str, action: dict) -> dict:
        info = read_msg(msg_path(box, folder, file).with_suffix(".txt"))
        if not info.get("msg_id"):
            raise VMError("Nachricht nicht gefunden.")
        r = ami.run({**action, "Context": "default", "Mailbox": box, "Folder": folder, "ID": info["msg_id"]})[0]
        if r.get("Response") != "Success":
            raise VMError(f"Asterisk: {r.get('Message', 'Fehler')}")
        try:
            ami.run({"Action": "VoicemailRefresh", "Context": "default", "Mailbox": box})
        except Exception:
            pass
        return {"ok": True}

    @app.delete("/api/voicemail/messages/<box>/<folder>/<file>")
    def delete_msg(box, folder, file):
        need_enabled()
        return jsonify(msg_action(box, folder, file, {"Action": "VoicemailRemove"}))

    @app.post("/api/voicemail/messages/<box>/<folder>/<file>/move")
    def move_msg(box, folder, file):
        need_enabled()
        to = "Old" if folder == "INBOX" else "INBOX"
        return jsonify(msg_action(box, folder, file, {"Action": "VoicemailMove", "ToFolder": to}))

    # fuer die Benutzerseite (portal.py)
    app.config["VOICEMAIL"] = {"settings": settings, "numbers": numbers, "mbid": mbid, "enabled": enabled,
                               "messages": lambda box: named(list_messages(spool, box)),
                               # fuer die Apps (phoneapp.py, /client/v1/voicemail): Datei, Loeschen, Verschieben
                               "msg_path": msg_path, "msg_action": msg_action}

    # Fax-Empfang und Sprachmenues (vmapps.py) nutzen denselben Asterisk
    import vmapps
    vmapps.register(app, router, apply, demo, {
        "ami": ami, "cfgdir": cfgdir, "spool": spool, "host_ip": host_ip, "enabled": enabled,
        "mail_ready": mail_ready, "numbers": numbers, "mbid": mbid, "codec_line": codec_line,
        "settings": settings})

    # Telefonbuch: Namen zu Rufnummern, Verzeichnis am Telefon, Kurzwahltasten
    import phonebook
    phonebook.register(app, router, apply, demo, cfgdir, host_ip)

    # Visual Voicemail am Telefon (XML-Dienst)
    import vvm
    vvm.register(app, router, demo, {
        "spool": spool, "list_messages": lambda sp, box: named(list_messages(sp, box)), "msg_action": msg_action,
        "settings": settings, "mbid": mbid, "host_ip": host_ip, "enabled": enabled})

    # Benutzerseite und Rechte (Administration / Benutzer)
    import portal
    portal.register(app, router, demo, cfgdir)

    # CME Phone (Softphone): APNs-Schluessel, Local-Push-Secret, Schnittstelle fuer die Apps
    import phoneapp
    phoneapp.register(app, router, demo, cfgdir)

    # Let's-Encrypt-Zertifikat für das SIP-Gateway (Kamailio), Tab Softphone
    import acme
    acme.register(app, demo, cfgdir, Path(os.environ.get("PHONEAPP_DATA_DIR", "/data/push")))

    # Konferenzen in Jitsi über Jigasi und Kamailio (Tab Jitsi)
    import jitsi
    jitsi.register(app, router, apply, demo, cfgdir, ami)

    # Shared Line fuer Geraete ohne eigenes Abo (z. B. Gigaset): stellvertretende dialog-Abos
    import sharedline
    sharedline.register(app, router, demo, cfgdir)
