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
FOLDERS = {"INBOX": "Neu", "Old": "Gehört"}


class VMError(ValueError):
    pass


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

    def settings() -> dict:
        s = {"pilot": "8000", "maxsecs": 180, "maxmsg": 100, "skip_pin": True, "mailboxes": {}}
        try:
            s.update(json.loads((cfgdir / "cme-voicemail.json").read_text()))
        except FileNotFoundError:
            pass
        return s

    def mbid(number: str) -> str:
        return re.sub(r"\D", "", number)

    def write_config(s: dict, names: dict[str, str]) -> bool:
        """Schreibt die Asterisk-Dateien; True, wenn sich die MWI-Ziele geaendert haben."""
        cfgdir.mkdir(parents=True, exist_ok=True)
        boxes = s["mailboxes"]
        mb_lines, mwi = [], []
        for number, b in sorted(boxes.items()):
            name = re.sub(r"[,|]", " ", names.get(number, "") or number)
            mb_lines.append(f"{mbid(number)} => {b['pin']},{name},,,")
            ep = f"vm-{mbid(number)}"
            mwi += [f"[{ep}]", "type=aor", f"contact=sip:{number}@{cme_ip}:5060", "",
                    f"[{ep}]", "type=endpoint", "transport=transport-udp", f"aors={ep}",
                    f"mailboxes={mbid(number)}@default", "context=from-cme", "disallow=all", "allow=alaw,ulaw", ""]
        files = {
            "mailboxes.conf": "\n".join(mb_lines) + "\n",
            "pjsip_mwi.conf": "\n".join(mwi) + "\n",
            "vm_general.conf": f"maxmsg={int(s['maxmsg'])}\nmaxsecs={int(s['maxsecs'])}\nminsecs=2\ntz=european\n",
            "globals.conf": f"VM_PILOT={s['pilot']}\nVM_MAIN_OPTS={'s' if s['skip_pin'] else ''}\n",
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
        return [n for n in res if n["number"]]

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
            dn = cme.get("dns", {}).get(n["tag"], {})
            msgs = list_messages(spool, mbid(n["number"])) if b else []
            boxes.append({"tag": n["tag"], "number": n["number"], "name": n["name"], "enabled": bool(b),
                          "pin": (b or {}).get("pin", ""), "timeout": (b or {}).get("timeout", 20),
                          "busy": (b or {}).get("busy", True), "id": mbid(n["number"]),
                          "cme": dn, "new": sum(m["folder"] == "INBOX" for m in msgs),
                          "old": sum(m["folder"] == "Old" for m in msgs), "messages": msgs})
        return jsonify(ok=True, enabled=True, host_ip=host_ip, settings={k: v for k, v in s.items() if k != "mailboxes"},
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
                  f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte", " codec g711alaw", " no vad"]
        # Umleitungen der Postfaecher auf die (neue) Nummer
        for n in numbers():
            b = s["mailboxes"].get(n["number"])
            if b:
                lines += dn_lines(n["tag"], b, s["pilot"], None)
        lines += ["voice register global", " create profile"]
        return apply(lines)

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
        if not tag.isdigit():
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
            b = {"pin": pin, "timeout": timeout, "busy": bool(d.get("busy"))}
            s["mailboxes"][n["number"]] = b
        else:
            b = None
            s["mailboxes"].pop(n["number"], None)
        restart = write_config(s, {x["number"]: x["name"] for x in nums})
        reload_asterisk(restart)
        cur = cme_state(s)["dns"].get(tag, {})
        res = apply(dn_lines(tag, b, s["pilot"], cur) + ["voice register global", " create profile"])
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
