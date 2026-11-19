"""
vmapps.py - Fax-Empfang und Sprachmenues (IVR) mit Asterisk fuer cme_web

Asterisk-Seite (Container cme-voicemail):
  cme/extensions_cme.conf   von hier erzeugt: [cme-numbers] mit den Fax- und IVR-Nummern,
                            je IVR ein Kontext [ivr-<id>]
  cme/sounds/ivr-<id>.wav   Ansagen (hochgeladen, mit ffmpeg auf 8 kHz mono umgerechnet)
  voicemail/fax/            empfangene Faxe als PDF + JSON (fax-mail.py), per Mail verschickt

Router-Seite: je Fax- und IVR-Nummer ein Dial-Peer zu Asterisk.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from flask import jsonify, request, send_file

RE_NUM = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
RE_NAME = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{1,40}$")
KEYS = [*"1234567890", "*", "#"]
ACTIONS = ["dial", "ivr", "queue", "voicemail", "repeat", "hangup"]
# Stimmen fuer die Sprachausgabe (Piper, laeuft lokal; Modelle werden beim ersten Gebrauch geladen)
TTS_VOICES = {
    "de_DE-thorsten-medium": "Thorsten (männlich)",
    "de_DE-thorsten-high": "Thorsten (männlich, beste Qualität, langsamer)",
    "de_DE-kerstin-low": "Kerstin (weiblich)",
    "de_DE-ramona-low": "Ramona (weiblich)",
    "de_DE-eva_k-x_low": "Eva (weiblich, einfach)",
}
FAX_DESC = "Fax (Asterisk)"
IVR_DESC = "IVR (Asterisk)"


class AppError(ValueError):
    pass


def register(app, router, apply, demo: bool, ctx: dict) -> dict:
    cfgdir: Path = ctx["cfgdir"]
    faxdir: Path = ctx["spool"] / "fax"
    sounds = cfgdir / "sounds"
    host_ip = ctx["host_ip"]
    ami = ctx["ami"]
    tts_dir = cfgdir / "tts-voices"
    tts_lock = threading.Lock()
    tts_cache: dict = {}

    # ---------------- Daten ----------------

    def data() -> dict:
        d = {"fax": [], "ivrs": []}
        try:
            d.update(json.loads((cfgdir / "cme-apps.json").read_text()))
        except FileNotFoundError:
            pass
        return d

    def dial_target(num: str) -> str:
        # Anruf zurueck an CME (Endpunkt "cme" in pjsip.conf)
        return f"Dial(PJSIP/{num}@cme,60)"

    def action_lines(a: dict, ivr: dict, mbid) -> list[str]:  # noqa: ARG001
        kind, target = a.get("action", ""), a.get("target", "")
        if kind == "dial":
            return ["Playback(transfer)", dial_target(target), "Hangup()"]
        if kind == "ivr":
            return [f"Goto(ivr-{target},s,1)"]
        if kind == "queue":
            return [f"Goto(queue-run,q{target},1)"]
        if kind == "voicemail":
            return [f"VoiceMail({mbid(target)}@default,u)", "Hangup()"]
        if kind == "repeat":
            return ["Goto(s,menu)"]
        return ["Playback(vm-goodbye)", "Hangup()"]

    def extensions(d: dict, dn_numbers: list[str]) -> str:
        mbid = ctx["mbid"]
        out = ["; Von cme-web erzeugt - Aenderungen hier gehen verloren.", "", "[cme-numbers]"]
        for f in d["fax"]:
            out += [f"exten => {f['number']},1,Set(FAXMAIL={f['email']})",
                    f" same => n,Set(FAXNAME={re.sub(r'[,()]', ' ', f['name']) or f['number']})",
                    f" same => n,Set(FAXNUM={f['number']})",
                    " same => n,Goto(cme-fax,s,1)"]
        for ivr in d["ivrs"]:
            if ivr.get("number"):
                out.append(f"exten => {ivr['number']},1,Goto(ivr-{ivr['id']},s,1)")
        for ivr in d["ivrs"]:
            i = ivr["id"]
            greet = f"/etc/asterisk/cme/sounds/ivr-{i}" if (sounds / f"ivr-{i}.wav").exists() else "beep"
            out += ["", f"; {ivr['name']}", f"[ivr-{i}]",
                    "exten => s,1,Answer()",
                    " same => n,Wait(0.5)",
                    f" same => n,Set(TIMEOUT(digit)={2 if ivr.get('direct') else 1})",
                    " same => n,Set(IVR_TRY=0)",
                    f" same => n(menu),Background({greet})",
                    f" same => n,WaitExten({int(ivr.get('timeout', 5))})"]
            for key, a in ivr["keys"].items():
                if a.get("action"):
                    lines = action_lines(a, ivr, mbid)
                    out.append(f"exten => {key},1,{lines[0]}")
                    out += [f" same => n,{l}" for l in lines[1:]]
            if ivr.get("direct"):
                # Durchwahl: nur die internen Rufnummern, keine beliebigen Ziele
                for num in dn_numbers:
                    if num not in ivr["keys"] or not ivr["keys"][num].get("action"):
                        out += [f"exten => {num},1,Playback(transfer)", f" same => n,{dial_target(num)}",
                                " same => n,Hangup()"]
            repeats = int(ivr.get("repeats", 2))
            for ex, act, pre in (("t", ivr.get("on_timeout") or {}, None),
                                 ("i", ivr.get("on_invalid") or {}, "Playback(invalid)")):
                out.append(f"exten => {ex},1,Set(IVR_TRY=$[${{IVR_TRY}} + 1])")
                if pre:
                    out.append(f" same => n,{pre}")
                out.append(f" same => n,GotoIf($[${{IVR_TRY}} < {repeats}]?s,menu)")
                out += [f" same => n,{l}" for l in action_lines(act, ivr, mbid)]
        return "\n".join(out) + "\n"

    def save(d: dict) -> None:
        cfgdir.mkdir(parents=True, exist_ok=True)
        dn_numbers = [n["number"] for n in ctx["numbers"]()]
        files = {"cme-apps.json": json.dumps(d, indent=2, ensure_ascii=False),
                 "extensions_cme.conf": extensions(d, dn_numbers)}
        for name, text in files.items():
            tmp = cfgdir / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(cfgdir / name)
        if not demo:
            ami.command("dialplan reload")

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

    def peer_lines(number: str, desc: str, fax: bool, existing: list[dict]) -> list[str]:
        p = next((x for x in existing if x["pattern"] == number and x["target"] == host_ip), None)
        used = {int(x["tag"]) for x in existing}
        tag = p["tag"] if p else str(next(t for t in range(8100, 9000) if t not in used))
        if not p:
            existing.append({"tag": tag, "pattern": number, "target": host_ip, "desc": desc})
        lines = [f"dial-peer voice {tag} voip", f" description {desc}", f" destination-pattern {number}",
                 " session protocol sipv2", f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte",
                 " no vad"]
        if fax:
            # Fax nur unkomprimiert durchreichen (Asterisk empfaengt per G.711)
            lines += [" codec g711alaw", " fax protocol pass-through g711alaw"]
        else:
            lines.append(ctx["codec_line"]())
        return lines

    def remove_peer(number: str, existing: list[dict]) -> list[str]:
        p = next((x for x in existing if x["pattern"] == number and x["target"] == host_ip
                  and x["desc"].startswith((FAX_DESC, IVR_DESC))), None)
        if not p:
            return []
        existing.remove(p)
        return [f"no dial-peer voice {p['tag']} voip"]

    def sync_router(old_numbers: dict[str, tuple[str, bool]], new_numbers: dict[str, tuple[str, bool]]) -> dict:
        """old/new: {nummer: (beschreibung, fax)} -> Dial-Peers anlegen/entfernen."""
        existing = peers()
        lines = []
        for num in old_numbers:
            if num not in new_numbers:
                lines += remove_peer(num, existing)
        for num, (desc, fax) in new_numbers.items():
            have = any(x["pattern"] == num and x["target"] == host_ip for x in existing)
            if have and old_numbers.get(num) == (desc, fax):
                continue      # unveraendert und am Router vorhanden
            lines += peer_lines(num, desc, fax, existing)
        return apply(lines) if lines else {"ok": True, "error": None, "config": []}

    def ascii_desc(text: str) -> str:
        # IOS-Beschreibungen nur in ASCII
        for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
            text = text.replace(a, b)
        return re.sub(r"[^A-Za-z0-9 _.,()/+\-]", "", text).strip()[:60]

    def routed(d: dict) -> dict[str, tuple[str, bool]]:
        r = {f["number"]: (ascii_desc(f"{FAX_DESC} {f['name'] or ''}"), True) for f in d["fax"]}
        r.update({i["number"]: (ascii_desc(f"{IVR_DESC} {i['name']}"), False) for i in d["ivrs"] if i.get("number")})
        return r

    def queue_list() -> list[dict]:
        try:
            return json.loads((cfgdir / "cme-queues.json").read_text()).get("queues", [])
        except FileNotFoundError:
            return []

    def check_free(number: str, d: dict, own: str) -> None:
        taken = {f["number"]: f"Fax {f['number']}" for f in d["fax"]}
        taken.update({i["number"]: f"IVR {i['name']}" for i in d["ivrs"] if i.get("number")})
        taken.update({q["number"]: f"Warteschlange {q['name']}" for q in queue_list() if q.get("number")})
        taken.pop(own, None)
        if number in taken:
            raise AppError(f"{number} ist schon vergeben ({taken[number]}).")
        if any(n["number"] == number for n in ctx["numbers"]()):
            raise AppError(f"{number} ist eine Rufnummer (DN) eines Telefons.")
        if number == ctx["settings"]()["pilot"]:
            raise AppError(f"{number} ist die Voicemail-Nummer.")

    def apply_change(old: dict, new: dict) -> dict:
        res = sync_router(routed(old), routed(new))
        if res["ok"]:
            save(new)
        return res

    # ---------------- Faxe ----------------

    def received() -> list[dict]:
        out = []
        for meta in sorted(faxdir.glob("*.json"), reverse=True) if faxdir.is_dir() else []:
            try:
                m = json.loads(meta.read_text())
            except Exception:
                continue
            m["id"] = meta.stem
            m["pdf"] = (faxdir / f"{meta.stem}.pdf").exists()
            out.append(m)
        return out

    # ---------------- API ----------------

    @app.errorhandler(AppError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    def need():
        if not ctx["enabled"]:
            raise AppError("Asterisk ist nicht eingerichtet (VM_HOST_IP, VM_AMI_SECRET).")

    @app.get("/static/vmapps.js")
    def vmapps_js():
        return send_file(Path(__file__).resolve().parent / "static" / "vmapps.js",
                         mimetype="text/javascript", max_age=0)

    @app.get("/api/vmapps")
    def get_apps():
        if not ctx["enabled"]:
            return jsonify(ok=True, enabled=False)
        d = data()
        try:
            ps = {x["pattern"]: x["tag"] for x in peers() if x["target"] == host_ip}
        except Exception:
            ps = {}
        for f in d["fax"]:
            f["dialpeer"] = ps.get(f["number"], "")
        for i in d["ivrs"]:
            i["dialpeer"] = ps.get(i.get("number", ""), "")
            i["greeting"] = (sounds / f"ivr-{i['id']}.wav").exists()
        boxes = [{"number": n, "id": ctx["mbid"](n)} for n in ctx["settings"]()["mailboxes"]]
        return jsonify(ok=True, enabled=True, mail=ctx["mail_ready"], fax=d["fax"], received=received(),
                       ivrs=d["ivrs"], numbers=ctx["numbers"](), mailboxes=boxes, keys=KEYS, tts=tts_info(),
                       queues=[{"id": q["id"], "name": q["name"]} for q in queue_list()])

    def tts_info() -> dict:
        voices = [{"id": v, "label": label, "installed": (tts_dir / f"{v}.onnx").exists()}
                  for v, label in TTS_VOICES.items()]
        return {"available": tts_available(), "voices": voices}

    # Fax-Nummern

    def fax_input(body: dict, d: dict, own: str) -> dict:
        number = str(body.get("number") or "").strip()
        if not RE_NUM.match(number):
            raise AppError("Ungueltige Faxnummer.")
        email = str(body.get("email") or "").strip()
        if not RE_EMAIL.match(email):
            raise AppError("Ungueltige E-Mail-Adresse.")
        name = str(body.get("name") or "").strip()
        if name and not RE_NAME.match(name):
            raise AppError("Name: Buchstaben, Ziffern und . , ( ) / + - erlaubt.")
        check_free(number, d, own)
        return {"number": number, "email": email, "name": name}

    @app.post("/api/vmapps/fax")
    def add_fax():
        need()
        d = data()
        old = json.loads(json.dumps(d))
        d["fax"].append(fax_input(request.get_json(force=True) or {}, d, ""))
        res = apply_change(old, d)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/vmapps/fax/<number>")
    def edit_fax(number):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        i = next((k for k, f in enumerate(d["fax"]) if f["number"] == number), None)
        if i is None:
            raise AppError(f"Faxnummer {number} gibt es nicht.")
        d["fax"][i] = fax_input(request.get_json(force=True) or {}, d, number)
        res = apply_change(old, d)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/vmapps/fax/<number>")
    def delete_fax(number):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        d["fax"] = [f for f in d["fax"] if f["number"] != number]
        res = apply_change(old, d)
        return jsonify(res), (200 if res["ok"] else 400)

    def fax_file(fid: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z._\-]{1,80}", fid):
            raise AppError("Ungueltiges Fax")
        return faxdir / fid

    @app.get("/api/vmapps/fax/files/<fid>.pdf")
    def fax_pdf(fid):
        need()
        p = fax_file(fid).with_suffix(".pdf")
        if not p.exists():
            return jsonify(ok=False, error="PDF nicht gefunden"), 404
        return send_file(p, mimetype="application/pdf", max_age=0, download_name=f"fax-{fid}.pdf",
                         as_attachment=request.args.get("download") == "1")

    @app.delete("/api/vmapps/fax/files/<fid>")
    def fax_delete(fid):
        need()
        for suffix in (".pdf", ".json", ".tif"):
            fax_file(fid).with_suffix(suffix).unlink(missing_ok=True)
        return jsonify(ok=True)

    # Sprachmenues

    def ivr_input(body: dict, d: dict, own: dict | None) -> dict:
        name = str(body.get("name") or "").strip()
        if not RE_NAME.match(name):
            raise AppError("Name fehlt oder enthaelt unzulaessige Zeichen.")
        number = str(body.get("number") or "").strip()
        if number:
            if not RE_NUM.match(number):
                raise AppError("Ungueltige Rufnummer fuer das Menue.")
            check_free(number, d, (own or {}).get("number", ""))
        try:
            timeout, repeats = int(body.get("timeout") or 5), int(body.get("repeats") or 2)
        except ValueError:
            raise AppError("Wartezeit und Wiederholungen muessen Zahlen sein.")
        if not 1 <= timeout <= 30 or not 1 <= repeats <= 5:
            raise AppError("Wartezeit 1-30 s, Wiederholungen 1-5.")
        dns = {n["number"] for n in ctx["numbers"]()}
        boxes = set(ctx["settings"]()["mailboxes"])
        ivr_ids = {i["id"] for i in d["ivrs"]}

        def act(a, where: str, allow_repeat: bool = True) -> dict:
            a = a or {}
            kind, target = str(a.get("action") or ""), str(a.get("target") or "").strip()
            if not kind:
                return {}
            if kind not in ACTIONS or (kind == "repeat" and not allow_repeat):
                raise AppError(f"{where}: ungueltige Aktion.")
            if kind == "dial":
                if not RE_NUM.match(target):
                    raise AppError(f"{where}: Zielnummer fehlt.")
                if target not in dns:
                    raise AppError(f"{where}: {target} ist keine interne Rufnummer.")
            elif kind == "ivr":
                if not target.isdigit() or int(target) not in ivr_ids:
                    raise AppError(f"{where}: Untermenue auswaehlen.")
                if own and int(target) == own["id"]:
                    raise AppError(f"{where}: Fuer dasselbe Menue \"wiederholen\" nehmen.")
            elif kind == "queue":
                if target not in {q["id"] for q in queue_list()}:
                    raise AppError(f"{where}: Warteschlange auswaehlen.")
            elif kind == "voicemail":
                if target not in boxes:
                    raise AppError(f"{where}: Postfach auswaehlen.")
            else:
                target = ""
            return {"action": kind, "target": target}

        keys = {}
        for k in KEYS:
            a = act((body.get("keys") or {}).get(k), f"Taste {k}")
            if a:
                keys[k] = a
        return {"name": name, "number": number, "timeout": timeout, "repeats": repeats,
                "direct": bool(body.get("direct")), "keys": keys,
                "on_timeout": act(body.get("on_timeout"), "Keine Eingabe", False) or {"action": "hangup"},
                "on_invalid": act(body.get("on_invalid"), "Falsche Eingabe", False) or {"action": "hangup"}}

    def find_ivr(d: dict, iid: str) -> dict:
        ivr = next((i for i in d["ivrs"] if str(i["id"]) == iid), None)
        if not ivr:
            raise AppError("Menue gibt es nicht.")
        return ivr

    @app.post("/api/vmapps/ivr")
    def add_ivr():
        need()
        d = data()
        old = json.loads(json.dumps(d))
        ivr = ivr_input(request.get_json(force=True) or {}, d, None)
        ivr["id"] = max([i["id"] for i in d["ivrs"]] + [0]) + 1
        d["ivrs"].append(ivr)
        res = apply_change(old, d)
        res["id"] = ivr["id"]
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/vmapps/ivr/<iid>")
    def edit_ivr(iid):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        cur = find_ivr(d, iid)
        new = ivr_input(request.get_json(force=True) or {}, d, cur)
        new["id"] = cur["id"]
        if cur.get("tts"):
            new["tts"] = cur["tts"]
        d["ivrs"] = [new if i is cur else i for i in d["ivrs"]]
        res = apply_change(old, d)
        res["id"] = cur["id"]
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/vmapps/ivr/<iid>")
    def delete_ivr(iid):
        need()
        d = data()
        old = json.loads(json.dumps(d))
        cur = find_ivr(d, iid)
        users = [i["name"] for i in d["ivrs"] if i is not cur and
                 any(a.get("action") == "ivr" and a.get("target") == str(cur["id"])
                     for a in [*i["keys"].values(), i.get("on_timeout") or {}, i.get("on_invalid") or {}])]
        if users:
            raise AppError(f"Das Menue wird noch verwendet von: {', '.join(users)}.")
        d["ivrs"] = [i for i in d["ivrs"] if i is not cur]
        res = apply_change(old, d)
        if res["ok"]:
            (sounds / f"ivr-{cur['id']}.wav").unlink(missing_ok=True)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.post("/api/vmapps/ivr/<iid>/greeting")
    def upload_greeting(iid):
        need()
        d = data()
        ivr = find_ivr(d, iid)
        f = request.files.get("file")
        if not f:
            raise AppError("Keine Datei.")
        raw = f.read()
        if not raw or len(raw) > 30 * 1024 * 1024:
            raise AppError("Datei leer oder groesser als 30 MB.")
        sounds.mkdir(parents=True, exist_ok=True)
        (sounds / f"ivr-{ivr['id']}.wav").write_bytes(to_phone(raw))
        ivr["greeting"] = True
        ivr.pop("tts", None)
        save(d)
        return jsonify(ok=True)

    @app.post("/api/vmapps/ivr/<iid>/greeting/tts")
    def tts_greeting(iid):
        need()
        d = data()
        ivr = find_ivr(d, iid)
        text, voice, speed = tts_input(request.get_json(force=True) or {})
        audio = to_phone(synthesize(text, voice, speed))
        sounds.mkdir(parents=True, exist_ok=True)
        (sounds / f"ivr-{ivr['id']}.wav").write_bytes(audio)
        ivr["greeting"] = True
        ivr["tts"] = {"text": text, "voice": voice, "speed": speed}
        save(d)
        return jsonify(ok=True)

    @app.post("/api/vmapps/tts/preview")
    def tts_preview():
        need()
        text, voice, speed = tts_input(request.get_json(force=True) or {})
        return to_phone(synthesize(text, voice, speed)), 200, {"Content-Type": "audio/wav", "Cache-Control": "no-store"}

    # ---------------- Sprachausgabe ----------------

    def to_phone(raw: bytes) -> bytes:
        """Beliebige Audiodatei -> WAV 8 kHz mono 16 bit (fuer Asterisk), Lautstaerke angeglichen."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AppError("ffmpeg fehlt auf dem Server.")
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in", Path(tmp) / "out.wav"
            src.write_bytes(raw)
            r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(src), "-t", "300", "-ac", "1", "-ar", "8000",
                                "-c:a", "pcm_s16le", "-af", "loudnorm=I=-18:TP=-2", str(dst)],
                               capture_output=True, timeout=120)
            if r.returncode != 0 or not dst.exists():
                raise AppError("Die Datei konnte nicht umgewandelt werden: " +
                               r.stderr.decode(errors="replace")[-300:])
            return dst.read_bytes()

    def tts_available() -> bool:
        try:
            import piper  # noqa: F401
            return True
        except ImportError:
            return False

    def tts_input(body: dict) -> tuple[str, str, float]:
        text = " ".join(str(body.get("text") or "").split())
        if not text:
            raise AppError("Text fehlt.")
        if len(text) > 1500:
            raise AppError("Höchstens 1500 Zeichen.")
        voice = str(body.get("voice") or "de_DE-thorsten-medium")
        if voice not in TTS_VOICES:
            raise AppError("Unbekannte Stimme.")
        try:
            speed = float(body.get("speed") or 1.0)
        except ValueError:
            raise AppError("Ungültiges Tempo.")
        if not 0.7 <= speed <= 1.4:
            raise AppError("Tempo zwischen 0,7 und 1,4.")
        return text, voice, round(speed, 2)

    def synthesize(text: str, voice: str, speed: float) -> bytes:
        """Text mit Piper vorlesen; die Stimme wird beim ersten Mal heruntergeladen (60-110 MB)."""
        try:
            import wave
            from piper import PiperVoice, SynthesisConfig
            from piper.download_voices import download_voice
        except ImportError:
            raise AppError("Die Sprachausgabe (piper-tts) ist auf dem Server nicht installiert.")
        with tts_lock:
            model = tts_dir / f"{voice}.onnx"
            if not model.exists() or not model.with_suffix(".onnx.json").exists():
                tts_dir.mkdir(parents=True, exist_ok=True)
                part = Path(tempfile.mkdtemp(dir=tts_dir, prefix=".dl-"))
                try:
                    download_voice(voice, part)
                    for f in part.iterdir():
                        f.replace(tts_dir / f.name)
                except Exception as e:
                    raise AppError(f"Die Stimme konnte nicht geladen werden: {type(e).__name__}: {e}")
                finally:
                    shutil.rmtree(part, ignore_errors=True)
            if voice not in tts_cache:
                tts_cache.clear()      # nur eine Stimme im Speicher halten
                tts_cache[voice] = PiperVoice.load(str(model))
            pv = tts_cache[voice]
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "tts.wav"
                with wave.open(str(out), "wb") as w:
                    pv.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.0 / speed))
                return out.read_bytes()

    @app.get("/api/vmapps/ivr/<iid>/greeting")
    def get_greeting(iid):
        need()
        ivr = find_ivr(data(), iid)
        p = sounds / f"ivr-{ivr['id']}.wav"
        if not p.exists():
            return jsonify(ok=False, error="Keine Ansage"), 404
        return send_file(p, mimetype="audio/wav", max_age=0)

    @app.delete("/api/vmapps/ivr/<iid>/greeting")
    def delete_greeting(iid):
        need()
        d = data()
        ivr = find_ivr(d, iid)
        (sounds / f"ivr-{ivr['id']}.wav").unlink(missing_ok=True)
        ivr["greeting"] = False
        ivr.pop("tts", None)
        save(d)
        return jsonify(ok=True)

    # Gespraechsaufzeichnung (recording.py) nutzt denselben Asterisk und die Sprachausgabe
    tools = {"tts_info": tts_info, "tts_input": tts_input, "synthesize": synthesize, "to_phone": to_phone}
    import recording
    recording.register(app, router, apply, demo, {**ctx, "tools": tools})
    # Warteschlangen mit Wartemusik (queues.py)
    import queues
    queues.register(app, router, apply, demo, ctx)
    return tools
