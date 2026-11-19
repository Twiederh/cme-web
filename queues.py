"""
queues.py - Warteschlangen (Asterisk app_queue) mit hochladbarer Wartemusik fuer cme_web

Asterisk-Seite (Container cme-voicemail, Volume /etc/asterisk/cme):
  cme/queues.conf               je Warteschlange [q-<id>] mit Strategie, Klingeldauer, Mitgliedern
  cme/musiconhold.conf          je Musik-Klasse [cme-<id>] (mode=files)
  cme/moh/<id>/<datei>.wav      Wartemusik, 8 kHz (G.711) und .sln16 (16 kHz, fuer G.722)
  cme/extensions_queue.conf     [cme-queues]: Nummer -> [queue-run] q<id> -> Queue(), danach Ausweichziel

Mitglieder sind Rufnummern von CME-Telefonen (Anruf zurueck ueber den Endpunkt "cme").
Router-Seite: je Warteschlange mit Nummer ein Dial-Peer zu Asterisk.
Wird aus vmapps.register() eingehaengt; Oberflaeche: static/queues.js (Tab Voicemail).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_NUM = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_NAME = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{1,40}$")
RE_ID = re.compile(r"^[0-9a-f]{1,12}$")
STRATEGIES = {
    "ringall": "Alle gleichzeitig",
    "linear": "Der Reihe nach (immer ab dem ersten)",
    "rrmemory": "Reihum (beim Nächsten weitermachen)",
    "leastrecent": "Wer am längsten keinen Anruf hatte",
    "fewestcalls": "Wer die wenigsten Anrufe hatte",
    "random": "Zufällig",
}
FALLBACKS = ["voicemail", "dial", "ivr", "hangup"]
QUEUE_DESC = "Warteschlange (Asterisk)"
MAX_MUSIC = 60 * 1024 * 1024
MAX_MUSIC_SECS = 1200


class QueueError(ValueError):
    pass


def ascii_text(text: str, n: int = 60) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^A-Za-z0-9 _.,()/+\-]", "", text).strip()[:n]


def parse_queue_show(out: str) -> dict[str, dict]:
    """queue show -> {q-<id>: {calls, completed, abandoned, holdtime, members: {nr: status}}}"""
    res, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^(q-[0-9a-f]+) has (\d+) calls?.*?\((\d+)s holdtime.*?C:(\d+), A:(\d+)", line)
        if m:
            cur = res[m.group(1)] = {"calls": int(m.group(2)), "holdtime": int(m.group(3)),
                                     "completed": int(m.group(4)), "abandoned": int(m.group(5)), "members": {}}
            continue
        mm = re.match(r"^\s+.*?\(PJSIP/([^@\s]+)@cme.*?\((Not in use|In use|Busy|Ringing|Unavailable|Invalid|"
                      r"On Hold|Unknown|paused)", line)
        if cur is not None and mm:
            cur["members"][mm.group(1)] = mm.group(2)
    return res


def register(app, router, apply, demo: bool, ctx: dict) -> None:
    cfgdir: Path = ctx["cfgdir"]
    mohdir = cfgdir / "moh"
    host_ip = ctx["host_ip"]
    ami = ctx["ami"]
    app.config["MAX_CONTENT_LENGTH"] = max(app.config.get("MAX_CONTENT_LENGTH") or 0, MAX_MUSIC)

    # ---------------- Daten ----------------

    def data() -> dict:
        d = {"queues": [], "music": []}
        try:
            d.update(json.loads((cfgdir / "cme-queues.json").read_text()))
        except FileNotFoundError:
            pass
        return d

    def apps() -> dict:
        try:
            return json.loads((cfgdir / "cme-apps.json").read_text())
        except FileNotFoundError:
            return {"fax": [], "ivrs": []}

    def dial_target(num: str, secs: int = 60) -> str:
        return f"Dial(PJSIP/{num}@cme,{secs})"

    def fallback_lines(a: dict) -> list[str]:
        kind, target = a.get("action", ""), a.get("target", "")
        if kind == "voicemail":
            return [f"VoiceMail({ctx['mbid'](target)}@default,u)", "Hangup()"]
        if kind == "dial":
            return [dial_target(target), "Hangup()"]
        if kind == "ivr":
            return [f"Goto(ivr-{target},s,1)"]
        return ["Playback(vm-goodbye)", "Hangup()"]

    def write_asterisk(d: dict) -> dict[str, str]:
        mohs = {m["id"]: m for m in d["music"]}
        q = ["; Von cme-web erzeugt - Aenderungen hier gehen verloren."]
        e = ["; Von cme-web erzeugt - Aenderungen hier gehen verloren.", "", "[cme-queues]"]
        for qu in d["queues"]:
            if qu.get("number"):
                e.append(f"exten => {qu['number']},1,Goto(queue-run,q{qu['id']},1)")
        e += ["", "[queue-run]"]
        for qu in d["queues"]:
            qid = qu["id"]
            moh = qu.get("music", "")
            q += ["", f"; {qu['name']}", f"[q-{qid}]", f"strategy={qu['strategy']}",
                  f"timeout={qu['ring']}", f"retry={qu['retry']}", "wrapuptime=0",
                  "joinempty=yes", "leavewhenempty=no", "ringinuse=yes", "autofill=yes",
                  "setinterfacevar=yes", "reportholdtime=no",
                  f"announce-position={'yes' if qu.get('announce') else 'no'}",
                  f"announce-frequency={60 if qu.get('announce') else 0}",
                  "announce-holdtime=no"]
            if moh and moh in mohs:
                q.append(f"musicclass=cme-{moh}")
            for num in qu["members"]:
                # bei "der Reihe nach" zaehlt die Reihenfolge der member-Zeilen
                q.append(f"member => PJSIP/{num}@cme,0,{num}")
            opts = "r" if moh == "ring" else ""
            wait = int(qu.get("maxwait") or 0)
            e += [f"exten => q{qid},1,Verbose(1,Warteschlange {ascii_text(qu['name'])} von ${{CALLERID(num)}})",
                  " same => n,Answer()" if moh != "ring" else " same => n,Ringing()",
                  f" same => n,Queue(q-{qid},{opts},,,{wait if wait else ''})",
                  f" same => n,Verbose(1,Warteschlange q-{qid} verlassen: ${{QUEUESTATUS}})"]
            e += [f" same => n,{l}" for l in fallback_lines(qu.get("fallback") or {})]
        m = ["; Von cme-web erzeugt - Aenderungen hier gehen verloren."]
        for mo in d["music"]:
            m += ["", f"; {mo['name']}", f"[cme-{mo['id']}]", "mode=files",
                  f"directory=/etc/asterisk/cme/moh/{mo['id']}",
                  f"sort={'random' if mo.get('shuffle') else 'alpha'}"]
        return {"queues.conf": "\n".join(q) + "\n", "extensions_queue.conf": "\n".join(e) + "\n",
                "musiconhold.conf": "\n".join(m) + "\n"}

    def save(d: dict, reload_moh: bool = False) -> None:
        cfgdir.mkdir(parents=True, exist_ok=True)
        files = {"cme-queues.json": json.dumps(d, indent=2, ensure_ascii=False), **write_asterisk(d)}
        for name, text in files.items():
            tmp = cfgdir / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(cfgdir / name)
        if not demo and ami:
            cmds = (["moh reload"] if reload_moh else []) + ["module reload app_queue.so", "dialplan reload"]
            for c in cmds:
                try:
                    ami.command(c)
                except Exception:
                    pass

    # ---------------- Router ----------------

    def peers() -> list[dict]:
        out = router.show("show running-config | section dial-peer voice")
        res, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^dial-peer voice (\d+)", line)
            if m:
                cur = {"tag": m.group(1), "pattern": "", "target": "", "desc": ""}
                res.append(cur)
                continue
            if cur is None:
                continue
            t = line.strip()
            if t.startswith("destination-pattern "):
                cur["pattern"] = t.split()[1]
            elif t.startswith("description "):
                cur["desc"] = t[12:]
            mm = re.match(r"session target ipv4:([\d.]+)", t)
            if mm:
                cur["target"] = mm.group(1)
        return res

    def sync_router(old: dict, new: dict) -> dict:
        """Dial-Peers fuer die Nummern der Warteschlangen anlegen, aendern, entfernen."""
        on = {q["number"]: ascii_text(f"{QUEUE_DESC} {q['name']}") for q in old["queues"] if q.get("number")}
        nn = {q["number"]: ascii_text(f"{QUEUE_DESC} {q['name']}") for q in new["queues"] if q.get("number")}
        existing = peers()
        used = {int(x["tag"]) for x in existing}
        lines = []
        for num in on:
            if num not in nn:
                p = next((x for x in existing if x["pattern"] == num and x["target"] == host_ip
                          and x["desc"].startswith(QUEUE_DESC)), None)
                if p:
                    lines.append(f"no dial-peer voice {p['tag']} voip")
        for num, desc in nn.items():
            p = next((x for x in existing if x["pattern"] == num and x["target"] == host_ip), None)
            if p and on.get(num) == desc:
                continue
            tag = p["tag"] if p else str(next(t for t in range(8100, 9000) if t not in used))
            used.add(int(tag))
            lines += [f"dial-peer voice {tag} voip", f" description {desc}", f" destination-pattern {num}",
                      " session protocol sipv2", f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte",
                      ctx["codec_line"](), " no vad"]
        return apply(lines) if lines else {"ok": True, "error": None, "config": []}

    def change(old: dict, new: dict) -> dict:
        res = sync_router(old, new)
        if res.get("ok"):
            save(new)
        res.pop("output", None)
        return res

    # ---------------- Eingaben ----------------

    def check_free(number: str, d: dict, own_id: str) -> None:
        a = apps()
        taken = {f["number"]: f"Fax {f['number']}" for f in a.get("fax", [])}
        taken.update({i["number"]: f"IVR {i['name']}" for i in a.get("ivrs", []) if i.get("number")})
        taken.update({q["number"]: f"Warteschlange {q['name']}" for q in d["queues"]
                      if q.get("number") and q["id"] != own_id})
        if number in taken:
            raise QueueError(f"{number} ist schon vergeben ({taken[number]}).")
        if any(n["number"] == number for n in ctx["numbers"]()):
            raise QueueError(f"{number} ist eine Rufnummer (DN) eines Telefons.")
        if number == ctx["settings"]()["pilot"]:
            raise QueueError(f"{number} ist die Voicemail-Nummer.")

    def queue_input(b: dict, d: dict, own_id: str) -> dict:
        name = str(b.get("name") or "").strip()
        if not RE_NAME.match(name):
            raise QueueError("Name fehlt oder enthält unzulässige Zeichen.")
        number = str(b.get("number") or "").strip()
        if number:
            if not RE_NUM.match(number):
                raise QueueError("Ungültige Rufnummer.")
            check_free(number, d, own_id)
        strategy = str(b.get("strategy") or "ringall")
        if strategy not in STRATEGIES:
            raise QueueError("Unbekannte Verteilung.")
        dns = {n["number"] for n in ctx["numbers"]()}
        members = []
        for m in b.get("members") or []:
            m = str(m).strip()
            if m not in dns:
                raise QueueError(f"{m} ist keine Rufnummer eines Telefons.")
            if m not in members:
                members.append(m)
        if not members:
            raise QueueError("Mindestens ein Telefon als Mitglied auswählen.")
        try:
            ring, retry, maxwait = int(b.get("ring") or 15), int(b.get("retry") or 3), int(b.get("maxwait") or 0)
        except ValueError:
            raise QueueError("Zeiten müssen Zahlen sein.")
        if not 5 <= ring <= 120 or not 1 <= retry <= 60 or not (maxwait == 0 or 10 <= maxwait <= 3600):
            raise QueueError("Klingeldauer 5–120 s, Pause 1–60 s, Höchstwartezeit 10–3600 s (0 = unbegrenzt).")
        music = str(b.get("music") or "")
        if music not in ("", "ring") and music not in {m["id"] for m in d["music"]}:
            raise QueueError("Unbekannte Wartemusik.")
        fb = b.get("fallback") or {}
        kind, target = str(fb.get("action") or "hangup"), str(fb.get("target") or "").strip()
        if kind not in FALLBACKS:
            raise QueueError("Ungültiges Ziel nach Ablauf der Wartezeit.")
        if kind == "voicemail" and target not in ctx["settings"]()["mailboxes"]:
            raise QueueError("Postfach für das Ausweichziel auswählen.")
        if kind == "dial" and target not in dns:
            raise QueueError("Telefon für das Ausweichziel auswählen.")
        if kind == "ivr" and target not in {str(i["id"]) for i in apps().get("ivrs", [])}:
            raise QueueError("Sprachmenü für das Ausweichziel auswählen.")
        if kind == "hangup":
            target = ""
        return {"name": name, "number": number, "strategy": strategy, "members": members, "ring": ring,
                "retry": retry, "maxwait": maxwait, "music": music, "announce": bool(b.get("announce")),
                "fallback": {"action": kind, "target": target}}

    def find(d: dict, key: str, qid: str) -> dict:
        x = next((x for x in d[key] if x["id"] == qid), None)
        if not x:
            raise QueueError("Nicht gefunden.")
        return x

    def need():
        if not ctx["enabled"]:
            raise QueueError("Asterisk ist nicht eingerichtet (VM_HOST_IP, VM_AMI_SECRET).")

    def convert(raw: bytes, dest: Path) -> int:
        """Audiodatei -> <dest>.wav (8 kHz) und <dest>.sln16 (16 kHz roh); gibt die Dauer in s zurueck."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise QueueError("ffmpeg fehlt auf dem Server.")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in"
            src.write_bytes(raw)
            norm = Path(tmp) / "norm.wav"
            r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-t", str(MAX_MUSIC_SECS), "-ac", "1",
                                "-ar", "16000", "-c:a", "pcm_s16le", "-af", "loudnorm=I=-20:TP=-3", str(norm)],
                               capture_output=True, timeout=300)
            if r.returncode != 0 or not norm.exists():
                raise QueueError("Die Datei konnte nicht umgewandelt werden: " +
                                 r.stderr.decode(errors="replace")[-300:])
            for args, suffix in ((["-ar", "8000", "-c:a", "pcm_s16le"], ".wav"),
                                 (["-ar", "16000", "-f", "s16le", "-c:a", "pcm_s16le"], ".sln16")):
                out = Path(tmp) / f"out{suffix}"
                r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(norm), *args, str(out)],
                                   capture_output=True, timeout=300)
                if r.returncode != 0:
                    raise QueueError("Umwandeln fehlgeschlagen: " + r.stderr.decode(errors="replace")[-300:])
                shutil.copyfile(out, dest.with_suffix(suffix))
            return int(norm.stat().st_size / 32000)

    def status() -> dict:
        if demo or not ami:
            return {}
        try:
            return parse_queue_show(ami.command("queue show"))
        except Exception:
            return {}

    # ---------------- API ----------------

    @app.errorhandler(QueueError)
    def on_q_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/queues.js")
    def queues_js():
        return send_file(BASE / "static" / "queues.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/queues")
    def q_list():
        if not ctx["enabled"]:
            return jsonify(ok=True, enabled=False)
        d = data()
        try:
            ps = {x["pattern"]: x["tag"] for x in peers() if x["target"] == host_ip}
        except Exception:
            ps = {}
        st = status()
        for q in d["queues"]:
            q["dialpeer"] = ps.get(q.get("number", ""), "")
            q["status"] = st.get(f"q-{q['id']}")
        boxes = list(ctx["settings"]()["mailboxes"])
        ivrs = [{"id": str(i["id"]), "name": i["name"]} for i in apps().get("ivrs", [])]
        return jsonify(ok=True, enabled=True, queues=d["queues"], music=d["music"], numbers=ctx["numbers"](),
                       mailboxes=boxes, ivrs=ivrs, strategies=STRATEGIES)

    @app.post("/api/queues")
    def q_add():
        need()
        d = data()
        old = json.loads(json.dumps(d))
        q = queue_input(request.get_json(force=True) or {}, d, "")
        q["id"] = uuid.uuid4().hex[:6]
        d["queues"].append(q)
        res = change(old, d)
        res["id"] = q["id"]
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/queues/<qid>")
    def q_edit(qid):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        cur = find(d, "queues", qid)
        new = queue_input(request.get_json(force=True) or {}, d, qid)
        cur.update(new)
        res = change(old, d)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/queues/<qid>")
    def q_delete(qid):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        q = find(d, "queues", qid)
        users = [i["name"] for i in apps().get("ivrs", [])
                 if any(a.get("action") == "queue" and a.get("target") == qid
                        for a in [*i.get("keys", {}).values(), i.get("on_timeout") or {}, i.get("on_invalid") or {}])]
        if users:
            raise QueueError(f"Die Warteschlange wird noch im Sprachmenü {', '.join(users)} verwendet.")
        d["queues"].remove(q)
        res = change(old, d)
        return jsonify(res), (200 if res["ok"] else 400)

    # Wartemusik

    @app.post("/api/queues/music")
    def m_add():
        need()
        b = request.get_json(force=True) or {}
        name = str(b.get("name") or "").strip()
        if not RE_NAME.match(name):
            raise QueueError("Name fehlt oder enthält unzulässige Zeichen.")
        d = data()
        m = {"id": uuid.uuid4().hex[:6], "name": name, "shuffle": bool(b.get("shuffle")), "files": []}
        d["music"].append(m)
        (mohdir / m["id"]).mkdir(parents=True, exist_ok=True)
        save(d, reload_moh=True)
        return jsonify(ok=True, id=m["id"])

    @app.put("/api/queues/music/<mid>")
    def m_edit(mid):
        need()
        b = request.get_json(force=True) or {}
        d = data()
        m = find(d, "music", mid)
        name = str(b.get("name") or m["name"]).strip()
        if not RE_NAME.match(name):
            raise QueueError("Name fehlt oder enthält unzulässige Zeichen.")
        m.update(name=name, shuffle=bool(b.get("shuffle")))
        save(d, reload_moh=True)
        return jsonify(ok=True)

    @app.delete("/api/queues/music/<mid>")
    def m_delete(mid):
        need()
        d = data()
        m = find(d, "music", mid)
        users = [q["name"] for q in d["queues"] if q.get("music") == mid]
        if users:
            raise QueueError(f"Die Wartemusik wird noch verwendet: {', '.join(users)}.")
        d["music"].remove(m)
        save(d, reload_moh=True)
        if RE_ID.match(mid):
            shutil.rmtree(mohdir / mid, ignore_errors=True)
        return jsonify(ok=True)

    @app.post("/api/queues/music/<mid>/files")
    def m_upload(mid):
        need()
        d = data()
        m = find(d, "music", mid)
        f = request.files.get("file")
        if not f or not f.filename:
            raise QueueError("Keine Datei.")
        raw = f.read()
        if not raw:
            raise QueueError("Die Datei ist leer.")
        fid = f"{int(time.time())}-{uuid.uuid4().hex[:4]}"
        target = mohdir / mid
        target.mkdir(parents=True, exist_ok=True)
        secs = convert(raw, target / fid)
        m["files"].append({"id": fid, "name": Path(f.filename).name[:80], "secs": secs})
        save(d, reload_moh=True)
        return jsonify(ok=True, id=fid, secs=secs)

    @app.get("/api/queues/music/<mid>/files/<fid>")
    def m_play(mid, fid):
        need()
        m = find(data(), "music", mid)
        if not any(x["id"] == fid for x in m["files"]):
            return jsonify(ok=False, error="Nicht gefunden"), 404
        return send_file(mohdir / mid / f"{fid}.wav", mimetype="audio/wav", max_age=0)

    @app.delete("/api/queues/music/<mid>/files/<fid>")
    def m_del_file(mid, fid):
        need()
        d = data()
        m = find(d, "music", mid)
        x = next((x for x in m["files"] if x["id"] == fid), None)
        if not x:
            raise QueueError("Datei nicht gefunden.")
        m["files"].remove(x)
        save(d, reload_moh=True)
        for suffix in (".wav", ".sln16"):
            (mohdir / mid / f"{fid}{suffix}").unlink(missing_ok=True)
        return jsonify(ok=True)
