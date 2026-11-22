"""
recording.py - Gespraechsaufzeichnung ueber den CUBE (SIPREC) fuer cme_web

Router:  media profile recorder / media class ... siprec an den gewaehlten Trunk-Dial-Peers,
         Dial-Peer zum Recorder (Container cme-recorder, UDP 5070),
         Dial-Peer fuer Anrufe von Asterisk (incoming uri via), damit Gespraeche ueber Asterisk
         nicht doppelt aufgezeichnet werden,
         optional Dial-Peer <prefix>T -> Asterisk fuer die Ansage vor eingehenden Gespraechen.
         Ansage automatisch: fuer jeden aufgezeichneten eingehenden Dial-Peer eine Kopie seines
         translation-profile incoming (Name <profil>-rec, Regelsatz 8190-8299), deren called-Regeln die
         Vorwahl voranstellen; der Dial-Peer wird auf die Kopie umgestellt (Ausschalten stellt ihn zurueck).
Asterisk: cme/extensions_rec.conf mit [cme-recnotice]: Ansage als Early Media, dann zur Nummer
          (Fax-Nummern ohne Ansage, IVR/Konferenz/Warteschlange direkt in Asterisk).
Recorder: liest cme-recording.json (Einstellungen, interne und eigene Rufnummern),
          schreibt Aufnahmen und .status.json nach VM_REC_DIR.
Ein/Aus waehrend des Gespraechs: .control.json in VM_REC_DIR (Weboberflaeche und Telefondienst /ipphone/rec);
Modus "demand" = jedes Gespraech beginnt ohne Aufnahme.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import wave
from pathlib import Path

from flask import Response, jsonify, request, send_file

import translation as _tr

RE_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
RE_NUM = re.compile(r"^\+?[0-9]{1,32}$")
RE_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
REC_PORT = 5070
REC_PATTERN = "89990001"          # Rufnummer des Recorders (nur fuer die SIP-Anfrage)
REC_DESC = "Aufzeichnung (cme-recorder)"
AST_DESC = "Von Asterisk (cme-web)"
NOTICE_DESC = "Aufzeichnungshinweis (Asterisk)"
NOTICE_SUFFIX = "-rec"               # Kopie des Uebersetzungsprofils mit Vorwahl
NOTICE_DEFAULT = "cme-rec-in"        # Kopie fuer Dial-Peers ohne eigenes Profil
NOTICE_RULES = range(8190, 8300)     # Regelsaetze der Kopien
DEFAULT_NOTICE = ("Dieses Gespräch wird aufgezeichnet. Wenn Sie damit nicht einverstanden sind, "
                  "beenden Sie bitte jetzt den Anruf.")


class RecError(ValueError):
    pass


def register(app, router, apply, demo: bool, ctx: dict) -> None:
    cfgdir: Path = ctx["cfgdir"]
    host_ip: str = ctx["host_ip"]
    ami = ctx["ami"]
    tools = ctx["tools"]          # Sprachausgabe aus vmapps.py
    sounds = cfgdir / "sounds"
    notice_wav = sounds / "rec-notice.wav"
    recdir = Path(os.environ.get("VM_REC_DIR", "/data/recordings"))
    if demo:
        recdir = Path(tempfile.gettempdir()) / "cme-web-demo-rec"
        demo_seed(recdir)

    # ---------------- Einstellungen ----------------

    def settings() -> dict:
        s = {"enabled": True, "transcript": True, "mail": False, "emails": {}, "default_email": "",
             "retention_days": 90, "min_secs": 3, "notice": True, "prefix": "8199", "notice_tts": None,
             "peers": [], "internal": [], "own": [], "mode": "always"}
        try:
            s.update(json.loads((cfgdir / "cme-recording.json").read_text()))
        except (FileNotFoundError, ValueError):
            pass
        return s

    def own_numbers() -> list[str]:
        """Eigene Rufnummern aus den Registrierungen am Provider (show sip-ua register status)."""
        try:
            out = router.show("show sip-ua register status")
        except Exception:
            return []
        return sorted({m.group(1) for m in re.finditer(r"^\s*(\+?\d{6,})\s", out, re.M)})

    def asterisk_conf(s: dict) -> str:
        p = s["prefix"]
        out = ["; Von cme-web erzeugt (Aufzeichnung) - Aenderungen hier gehen verloren.", "",
               "; Eingehende Gespraeche mit vorangestellter Nummer: Hinweis abspielen, dann zur Nummer",
               "[cme-recnotice]",
               f"exten => _{p}X.,1,Set(RNUM=${{EXTEN:{len(p)}}})",
               " same => n,Goto(cme-recnotice-run,s,1)"]
        # Rufnummern mit + (z. B. +4961872024990): der Router setzt 8199 vor die Ziffern ohne +,
        # Asterisk stellt das + beim Weiterverbinden wieder voran
        for n in sorted({x for x in s.get("internal") or [] if x.startswith("+") and x[1:].isdigit()}):
            out += [f"exten => {p}{n[1:]},1,Set(RNUM={n})", " same => n,Goto(cme-recnotice-run,s,1)"]
        # Faxe ohne Ansage (die wuerde die Faxuebertragung stoeren)
        try:
            faxes = [f["number"] for f in json.loads((cfgdir / "cme-apps.json").read_text()).get("fax", [])]
        except (FileNotFoundError, ValueError):
            faxes = []
        for n in sorted(set(faxes)):
            out += [f"exten => {p}{n.lstrip('+')},1,Goto(cme-numbers,{n},1)"]
        out += ["", "[cme-recnotice-run]",
                "exten => s,1,Verbose(1,Aufzeichnungshinweis fuer ${RNUM} von ${CALLERID(num)})",
                " same => n,Set(CHANNEL(tonezone)=de)"]
        if s.get("notice") and notice_wav.exists():
            out += [" same => n,Progress()", " same => n,Wait(0.3)",
                    f" same => n,Playback({notice_wav.with_suffix('')},noanswer)"]
        # Sprachmenue, Konferenz, Warteschlange: gleich in Asterisk weiter, sonst zurueck an CME
        for c in ("cme-numbers", "cme-conf", "cme-queues"):
            out.append(f" same => n,GotoIf(${{DIALPLAN_EXISTS({c},${{RNUM}},1)}}?{c},${{RNUM}},1)")
        out += [" same => n,Dial(PJSIP/${RNUM}@cme,180,r)", " same => n,Hangup()"]
        return "\n".join(out) + "\n"

    def save(s: dict, refresh: bool = True) -> None:
        if refresh:
            try:
                s["internal"] = [n["number"] for n in ctx["numbers"]()]
                s["own"] = own_numbers()
                s["vm_emails"] = {k: v["email"] for k, v in ctx["settings"]()["mailboxes"].items() if v.get("email")}
            except Exception:
                pass
        cfgdir.mkdir(parents=True, exist_ok=True)
        for name, text in (("cme-recording.json", json.dumps(s, indent=2, ensure_ascii=False)),
                           ("extensions_rec.conf", asterisk_conf(s))):
            tmp = cfgdir / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(cfgdir / name)
        if not demo and ami is not None:
            try:
                ami.command("dialplan reload")
            except Exception:
                pass

    # ---------------- Router ----------------

    def parse_peers(out: str) -> list[dict]:
        res, cur = [], None
        for line in out.splitlines():
            m = re.match(r"^dial-peer voice (\d+) (\w+)", line)
            if m:
                cur = {"tag": m.group(1), "kind": m.group(2), "desc": "", "pattern": "", "target": "",
                       "media_class": "", "incoming": "", "codec": "", "tp_in": ""}
                res.append(cur)
                continue
            if cur is None or not line.startswith(" "):
                cur = None if not line.startswith(" ") else cur
                continue
            t = line.strip()
            if t.startswith("description "):
                cur["desc"] = t[12:]
            elif t.startswith("destination-pattern "):
                cur["pattern"] = t.split()[1]
            elif t.startswith("session target "):
                cur["target"] = t.split()[2]
            elif t.startswith("media-class "):
                cur["media_class"] = t.split()[1]
            elif t.startswith(("incoming called-number", "incoming uri", "incoming calling")):
                cur["incoming"] = (cur["incoming"] + ", " if cur["incoming"] else "") + t[9:]
            elif t.startswith("voice-class codec "):
                cur["codec"] = t.split()[2]
            elif t.startswith("translation-profile incoming "):
                cur["tp_in"] = t.split()[2]
        return res

    def parse_media(out: str) -> tuple[dict, dict]:
        profiles, classes, cur = {}, {}, None
        for line in out.splitlines():
            m = re.match(r"^media profile recorder (\d+)", line)
            mc = re.match(r"^media class (\d+)", line)
            if m:
                cur = profiles.setdefault(m.group(1), {"peers": []})
            elif mc:
                cur = classes.setdefault(mc.group(1), {"profile": "", "siprec": False})
            elif line.startswith(" ") and cur is not None:
                t = line.split()
                if t[:1] == ["media-recording"]:
                    cur["peers"] = t[1:]
                elif t[:2] == ["recorder", "profile"] and len(t) > 2:
                    cur["profile"], cur["siprec"] = t[2], "siprec" in t
            else:
                cur = None
        return profiles, classes

    def codec_lists() -> dict[int, list[str]]:
        out = router.show("show running-config | section voice class codec")
        lists, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^voice class codec\s+(\d+)", line)
            if m:
                cur = lists.setdefault(int(m.group(1)), [])
                continue
            mm = re.match(r"^\s+codec preference \d+ (\S+)", line)
            if cur is not None and mm:
                cur.append(mm.group(1))
        return lists

    def router_state(s: dict) -> dict:
        if demo:
            st = s.get("demo_router") or {}
            peers = parse_peers(router.dialpeers)
            for p in peers:
                p["media_class"] = st.get("mclass", "") if p["tag"] in st.get("peers", []) else ""
            return {"peers": [dict(p, recorded=bool(p["media_class"])) for p in peers if p["kind"] == "voip"],
                    "rec_peer": st.get("rec_peer", ""), "ast_peer": st.get("ast_peer", ""),
                    "notice_peer": st.get("notice_peer", ""), "mclass": st.get("mclass", ""),
                    "profile": st.get("mclass", ""), "codec": "", "uri": "", "configured": bool(st)}
        peers = parse_peers(router.show("show running-config | section dial-peer voice"))
        profiles, classes = parse_media(router.show("show running-config | section ^media"))
        rec = next((p for p in peers if p["target"] == f"ipv4:{host_ip}:{REC_PORT}"), None)
        ast = next((p for p in peers if p["desc"] == AST_DESC), None)
        notice = next((p for p in peers if p["desc"] == NOTICE_DESC), None)
        profile = next((t for t, p in profiles.items() if rec and rec["tag"] in p["peers"]), "")
        mclass = next((t for t, c in classes.items() if profile and c["profile"] == profile), "")
        uri = ""
        if ast:
            m = re.search(r"uri via (\d+)", ast["incoming"])
            uri = m.group(1) if m else ""
        ours = {x["tag"] for x in (rec, ast, notice) if x}
        cand = [dict(p, recorded=bool(mclass) and p["media_class"] == mclass) for p in peers
                if p["kind"] == "voip" and p["tag"] not in ours and host_ip not in p["target"]]
        return {"peers": cand, "rec_peer": rec["tag"] if rec else "", "ast_peer": ast["tag"] if ast else "",
                "notice_peer": notice["tag"] if notice else "", "mclass": mclass, "profile": profile,
                "codec": rec["codec"] if rec else "", "uri": uri,
                "configured": bool(rec and mclass and classes[mclass]["siprec"])}

    # ---------------- Ansage: Vorwahl per Kopie des eingehenden Uebersetzungsprofils ----------------

    def notice_rules(prefix: str, base: list[dict]) -> list[dict]:
        """called-Regeln des Originals mit Vorwahl vor dem Ergebnis (+ entfaellt, Asterisk setzt es wieder)."""
        out = []
        for r in base:
            r = dict(r)
            if not r["reject"]:
                rp = r["replace"]
                rp = rp[2:] if rp.startswith("\\+") else (rp[1:] if rp.startswith("+") else rp)
                if rp and not rp.startswith(prefix):      # schon von Hand vorangestellt: so lassen
                    r["replace"] = prefix + rp
            out.append(r)
        nxt = max([r["prec"] for r in out] + [0]) + 1
        # alles, was keine Regel trifft, bekommt die Vorwahl ebenfalls
        for match in (r"^\+\(.*\)", r"^\(.*\)"):
            if nxt <= 15:
                out.append({"prec": nxt, "reject": False, "match": match, "replace": prefix + r"\1",
                            "type_from": "", "type_to": "", "plan_from": "", "plan_to": ""})
                nxt += 1
        return out

    def notice_lines(prefix: str, peers: list[dict], recorded: set[str], enable: bool, tr_out: str) -> list[str]:
        """Befehle, damit die aufgezeichneten eingehenden Dial-Peers (ohne session target) die Kopie nutzen."""
        rules, profiles = _tr.parse_translation(tr_out)

        def ours(name: str) -> bool:
            pr = profiles.get(name)
            if not pr or not (name.endswith(NOTICE_SUFFIX) or name == NOTICE_DEFAULT):
                return False
            t = pr["translate"].get("called", "")
            return t.isdigit() and int(t) in NOTICE_RULES

        def orig_of(name: str) -> str:
            return "" if name == NOTICE_DEFAULT else name[:-len(NOTICE_SUFFIX)]

        want, peer_lines = {}, []
        for p in peers:
            if p["kind"] != "voip" or p["target"] or not p["incoming"]:
                continue                      # nur eingehende Dial-Peers
            cur = p["tp_in"]
            orig = orig_of(cur) if ours(cur) else cur
            if enable and p["tag"] in recorded:
                copy = f"{orig}{NOTICE_SUFFIX}" if orig else NOTICE_DEFAULT
                if len(copy) > 30:
                    raise RecError(f"Profilname {copy} ist zu lang (Dial-Peer {p['tag']}).")
                want[copy] = orig
                if cur != copy:
                    peer_lines += [f"dial-peer voice {p['tag']} voip", f" translation-profile incoming {copy}"]
            elif ours(cur):
                peer_lines += [f"dial-peer voice {p['tag']} voip",
                               f" translation-profile incoming {orig}" if orig else f" no translation-profile incoming {cur}"]
        used = {int(t) for t in rules}
        rule_lines, prof_lines = [], []
        for copy, orig in sorted(want.items()):
            op = profiles.get(orig, {"translate": {}}) if orig else {"translate": {}}
            base = rules.get(op["translate"].get("called", ""), {}).get("rules", [])
            new = notice_rules(prefix, base)
            have = profiles.get(copy) if ours(copy) else None
            rtag = have["translate"]["called"] if have else None
            if not rtag:
                free = next((t for t in NOTICE_RULES if t not in used), None)
                if free is None:
                    raise RecError("Kein freier Regelsatz (8190-8299) fuer die Ansage.")
                used.add(free)
                rtag = str(free)
            rl = _tr.ruleset_lines(rtag, new, rules.get(rtag, {}).get("rules"))
            if len(rl) > 1:
                rule_lines += rl
            tw = {k: v for k, v in op["translate"].items() if k != "called"}
            tw["called"] = rtag
            th = (have or {}).get("translate", {})
            if copy in profiles and not have:
                raise RecError(f"Es gibt schon ein Uebersetzungsprofil {copy}, das nicht von cme-web ist.")
            pl = [f" no translate {k}" for k in th if k not in tw]
            pl += [f" translate {k} {v}" for k, v in tw.items() if th.get(k) != v]
            if pl:
                prof_lines += [f"voice translation-profile {copy}"] + pl
        cleanup = []
        for name in sorted(profiles):
            if ours(name) and name not in want:
                cleanup += [f"no voice translation-profile {name}",
                            f"no voice translation-rule {profiles[name]['translate']['called']}"]
        return rule_lines + prof_lines + peer_lines + cleanup

    def notice_router(s: dict, enable: bool | None = None) -> list[str]:
        """Befehle fuer die Ansage am Router passend zu den aktuell aufgezeichneten Dial-Peers."""
        if demo:
            return []
        st = router_state(s)
        peers = parse_peers(router.show("show running-config | section dial-peer voice"))
        recorded = {p["tag"] for p in st["peers"] if p["recorded"]}
        # nur mit Dial-Peer <vorwahl>T zu Asterisk, sonst liefen die Anrufe ins Leere
        on = (bool(s.get("notice")) and st["configured"] and bool(st["notice_peer"])) if enable is None else enable
        return notice_lines(s["prefix"], peers, recorded, on,
                            router.show("show running-config | section voice translation"))

    def free_tag(used: set[int], start: int, stop: int, step: int = 1) -> int:
        t = next((t for t in range(start, stop, step) if t not in used), None)
        if t is None:
            raise RecError("Keine freie Nummer (Tag) mehr frei.")
        used.add(t)
        return t

    def setup_lines(s: dict, st: dict, peers: list[str]) -> list[str]:
        used_peers = {int(t) for t in re.findall(r"^dial-peer voice (\d+)", router.show(
            "show running-config | section dial-peer voice"), re.M)}
        codecs = codec_lists()
        rec = st["rec_peer"] or str(free_tag(used_peers, 8190, 9000))
        ast = st["ast_peer"] or str(free_tag(used_peers, 8190, 9000))
        codec = st["codec"] or str(free_tag(set(codecs), 8999, 8000, -1))
        uri = st["uri"] or "8999"
        mtag = st["mclass"] or st["profile"] or "8999"
        cl = ctx["codec_line"]()
        lines = [f"voice class codec {codec}", " codec preference 1 g711alaw", " codec preference 2 g711ulaw",
                 " codec preference 3 g722-64",
                 f"voice class uri {uri} sip", f" host ipv4:{host_ip}",
                 f"dial-peer voice {rec} voip", f" description {REC_DESC}", f" destination-pattern {REC_PATTERN}",
                 " session protocol sipv2", f" session target ipv4:{host_ip}:{REC_PORT}",
                 " session transport udp", f" voice-class codec {codec}", " no vad",
                 f"dial-peer voice {ast} voip", f" description {AST_DESC}", " session protocol sipv2",
                 f" incoming uri via {uri}", " dtmf-relay rtp-nte", cl, " no vad",
                 # Umleitungen (z. B. auf die Voicemail) selbst ausfuehren statt 302/REFER an Asterisk
                 " no supplementary-service sip moved-temporarily", " no supplementary-service sip refer"]
        if s.get("notice"):
            notice = st["notice_peer"] or str(free_tag(used_peers, 8190, 9000))
            lines += [f"dial-peer voice {notice} voip", f" description {NOTICE_DESC}",
                      f" destination-pattern {s['prefix']}T", " session protocol sipv2",
                      f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte", cl, " no vad"]
        elif st["notice_peer"]:
            lines.append(f"no dial-peer voice {st['notice_peer']} voip")
        lines += [f"media profile recorder {mtag}", " media-type audio", f" media-recording {rec}",
                  f"media class {mtag}", f" recorder profile {mtag} siprec"]
        for p in st["peers"]:
            if p["tag"] in peers:
                lines += [f"dial-peer voice {p['tag']} voip", f" media-class {mtag}"]
            elif p["recorded"]:
                lines += [f"dial-peer voice {p['tag']} voip", f" no media-class {mtag}"]
        if not demo:
            lines += notice_lines(s["prefix"], parse_peers(router.show("show running-config | section dial-peer voice")),
                                  set(peers), bool(s.get("notice")),
                                  router.show("show running-config | section voice translation"))
        return lines

    def remove_lines(st: dict) -> list[str]:
        lines = notice_router(settings(), enable=False)      # Dial-Peers zurueck auf ihre Profile
        for p in st["peers"]:
            if p["recorded"]:
                lines += [f"dial-peer voice {p['tag']} voip", f" no media-class {st['mclass']}"]
        if st["mclass"]:
            lines.append(f"no media class {st['mclass']}")
        if st["profile"]:
            lines.append(f"no media profile recorder {st['profile']}")
        for t in (st["rec_peer"], st["ast_peer"], st["notice_peer"]):
            if t:
                lines.append(f"no dial-peer voice {t} voip")
        if st["uri"]:
            lines.append(f"no voice class uri {st['uri']}")
        if st["codec"]:
            lines.append(f"no voice class codec {st['codec']}")
        return lines

    # ---------------- Aufnahmen ----------------

    def recorder_status() -> dict:
        try:
            st = json.loads((recdir / ".status.json").read_text())
        except (FileNotFoundError, ValueError):
            return {"online": False}
        st["online"] = time.time() - st.get("time", 0) < 90
        return st

    def recordings(q: str = "", limit: int = 300) -> list[dict]:
        out = []
        files = sorted(recdir.glob("*.json"), reverse=True) if recdir.is_dir() else []
        q = q.strip().lower()
        for p in files:
            if not RE_ID.match(p.stem):
                continue          # .status.json, .control.json usw. sind keine Aufnahmen
            try:
                m = json.loads(p.read_text())
            except (ValueError, OSError):
                continue
            if q and q not in json.dumps(m, ensure_ascii=False).lower():
                continue
            m["id"] = p.stem
            out.append(m)
            if len(out) >= limit:
                break
        return out

    def rec_file(rid: str) -> Path:
        if not RE_ID.match(rid):
            raise RecError("Ungültige Aufnahme.")
        return recdir / rid

    # ---------------- API ----------------

    @app.errorhandler(RecError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    def need():
        if not ctx["enabled"]:
            raise RecError("Asterisk/Voicemail ist nicht eingerichtet (VM_HOST_IP, VM_AMI_SECRET).")

    @app.get("/static/recording.js")
    def rec_recording_js():
        return send_file(Path(__file__).resolve().parent / "static" / "recording.js",
                         mimetype="text/javascript", max_age=0)

    @app.get("/api/recording")
    def rec_get_recording():
        if not ctx["enabled"]:
            return jsonify(ok=True, enabled=False)
        s = settings()
        err = ""
        try:
            st = router_state(s)
            nums = ctx["numbers"]()
            if [n["number"] for n in nums] != s.get("internal"):
                save(s)       # neue Rufnummern an den Recorder weitergeben
        except Exception as e:
            st, nums, err = {"peers": [], "configured": False}, [], f"{type(e).__name__}: {e}"
        sync = notice_sync(s, st) if not err else None
        vm = ctx["settings"]()["mailboxes"]
        s.pop("demo_router", None)
        return jsonify(ok=True, enabled=True, settings=s, router=st, router_error=err, host_ip=host_ip,
                       recorder=recorder_status(), active=active_calls(), numbers=nums, mail_ready=ctx["mail_ready"],
                       vm_emails={k: v.get("email", "") for k, v in vm.items() if v.get("email")},
                       notice={"exists": notice_wav.exists()}, tts=tools["tts_info"](),
                       recordings=recordings(request.args.get("q", "")), notice_sync=sync)

    notice_state = {"failed": "", "last": None}

    def notice_sync(s: dict, st: dict) -> dict | None:
        """Kopien der Uebersetzungsregeln nachziehen, wenn sich das Original geaendert hat."""
        if demo or not st.get("configured"):
            return notice_state["last"]
        try:
            lines = notice_router(s)
        except Exception as e:
            notice_state["last"] = {"ok": False, "error": str(e)}
            return notice_state["last"]
        key = "\n".join(lines)
        if lines and key != notice_state["failed"]:
            r = apply(lines)
            r.pop("output", None)
            notice_state["failed"] = "" if r.get("ok") else key
            notice_state["last"] = {"ok": r.get("ok"), "error": r.get("error"), "config": r.get("config", lines)}
        return notice_state["last"]

    @app.put("/api/recording/settings")
    def rec_put_settings():
        need()
        b = request.get_json(force=True) or {}
        s = settings()
        try:
            days, mins = int(b.get("retention_days") or 0), int(b.get("min_secs") or 0)
        except ValueError:
            raise RecError("Aufbewahrung und Mindestlänge müssen Zahlen sein.")
        if not 0 <= days <= 3650 or not 0 <= mins <= 60:
            raise RecError("Aufbewahrung 0–3650 Tage (0 = unbegrenzt), Mindestlänge 0–60 s.")
        emails = {}
        for num, mail in (b.get("emails") or {}).items():
            num, mail = str(num).strip(), str(mail or "").strip()
            if not mail:
                continue
            if not RE_NUM.match(num):
                raise RecError(f"Ungültige Rufnummer: {num}")
            if not RE_EMAIL.match(mail):
                raise RecError(f"Ungültige E-Mail-Adresse: {mail}")
            emails[num] = mail
        default = str(b.get("default_email") or "").strip()
        if default and not RE_EMAIL.match(default):
            raise RecError("Ungültige Standard-E-Mail-Adresse.")
        prefix = str(b.get("prefix") or s["prefix"]).strip()
        if not re.fullmatch(r"[0-9]{2,6}", prefix):
            raise RecError("Vorwahl für die Ansage: 2–6 Ziffern.")
        if any(n["number"].startswith(prefix) for n in ctx["numbers"]()):
            raise RecError(f"Mit {prefix} beginnt schon eine interne Rufnummer.")
        router_change = prefix != s["prefix"] or bool(b.get("notice")) != bool(s.get("notice"))
        mode = str(b.get("mode") or s.get("mode") or "always")
        if mode not in ("always", "demand"):
            raise RecError("Modus: always oder demand.")
        s.update(mode=mode)
        s.update(enabled=bool(b.get("enabled")), transcript=bool(b.get("transcript")), mail=bool(b.get("mail")),
                 emails=emails, default_email=default, retention_days=days, min_secs=mins,
                 notice=bool(b.get("notice")), prefix=prefix)
        save(s)
        res = {"ok": True, "router_change": False, "notice_router": None}
        if router_change and not demo:
            try:
                st = router_state(s)
                if st["configured"]:
                    # Dial-Peer <vorwahl>T zu Asterisk und Uebersetzungsprofile fuer die Ansage gleich mit anpassen
                    r = apply(setup_lines(s, st, [p["tag"] for p in st["peers"] if p["recorded"]]))
                    r.pop("output", None)
                    res["notice_router"] = r
            except RecError as e:
                res["notice_router"] = {"ok": False, "error": str(e)}
        return jsonify(res)

    @app.post("/api/recording/router")
    def rec_setup_router():
        need()
        s = settings()
        b = request.get_json(force=True) or {}
        st = router_state(s)
        tags = {p["tag"] for p in st["peers"]}
        peers = [str(t) for t in b.get("peers") or [] if str(t) in tags]
        if not peers:
            raise RecError("Mindestens einen Dial-Peer zum Provider auswählen.")
        lines = setup_lines(s, st, peers)
        if demo:
            res = apply(lines)
            s["demo_router"] = {"peers": peers, "mclass": "8999", "rec_peer": "8190", "ast_peer": "8191",
                                "notice_peer": "8192" if s.get("notice") else ""}
        else:
            res = apply(lines)
        if res["ok"]:
            s["peers"] = peers
            save(s)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/recording/router")
    def rec_remove_router():
        need()
        s = settings()
        st = router_state(s)
        lines = remove_lines(st) if not demo else ["no media class 8999"]
        res = apply(lines) if lines else {"ok": True, "error": None, "config": []}
        if res["ok"]:
            s.pop("demo_router", None)
            s["peers"] = []
            save(s)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.get("/api/recording/files/<rid>")
    def rec_get_file(rid):
        need()
        base = rec_file(rid)
        p = next((base.with_suffix(x) for x in (".mp3", ".wav") if base.with_suffix(x).exists()), None)
        if not p:
            return jsonify(ok=False, error="Aufnahme nicht gefunden"), 404
        return send_file(p, mimetype="audio/mpeg" if p.suffix == ".mp3" else "audio/wav", max_age=0,
                         conditional=True, download_name=f"gespraech-{rid}{p.suffix}",
                         as_attachment=request.args.get("download") == "1")

    @app.delete("/api/recording/files/<rid>")
    def rec_delete_file(rid):
        need()
        base = rec_file(rid)
        for x in (".mp3", ".wav", ".json", ".txt"):
            base.with_suffix(x).unlink(missing_ok=True)
        return jsonify(ok=True)

    # Ansage

    @app.post("/api/recording/notice/tts")
    def rec_notice_tts():
        need()
        text, voice, speed = tools["tts_input"](request.get_json(force=True) or {})
        audio = tools["to_phone"](tools["synthesize"](text, voice, speed))
        sounds.mkdir(parents=True, exist_ok=True)
        notice_wav.write_bytes(audio)
        s = settings()
        s["notice_tts"] = {"text": text, "voice": voice, "speed": speed}
        save(s, refresh=False)
        return jsonify(ok=True)

    @app.post("/api/recording/notice")
    def rec_notice_upload():
        need()
        f = request.files.get("file")
        raw = f.read() if f else b""
        if not raw or len(raw) > 30 * 1024 * 1024:
            raise RecError("Datei fehlt oder ist größer als 30 MB.")
        sounds.mkdir(parents=True, exist_ok=True)
        notice_wav.write_bytes(tools["to_phone"](raw))
        s = settings()
        s["notice_tts"] = None
        save(s, refresh=False)
        return jsonify(ok=True)

    @app.get("/api/recording/notice")
    def rec_notice_get():
        need()
        if not notice_wav.exists():
            return jsonify(ok=False, error="Keine Ansage"), 404
        return send_file(notice_wav, mimetype="audio/wav", max_age=0)

    @app.delete("/api/recording/notice")
    def rec_notice_delete():
        need()
        notice_wav.unlink(missing_ok=True)
        s = settings()
        s["notice_tts"] = None
        save(s, refresh=False)
        return jsonify(ok=True)

    # ---------------- Aufnahme ein/aus waehrend des Gespraechs ----------------
    # cme-web schreibt recdir/.control.json ({aufnahme-id: true|false}); der Recorder liest sie zweimal je Sekunde.

    ctl_lock = threading.Lock()

    def set_control(rid: str, on: bool) -> None:
        with ctl_lock:
            path = recdir / ".control.json"
            try:
                cur = json.loads(path.read_text() or "{}")
            except (FileNotFoundError, ValueError):
                cur = {}
            active = {a["id"] for a in recorder_status().get("active", [])}
            cur = {k: v for k, v in cur.items() if k in active}     # beendete Gespraeche aufraeumen
            cur[rid] = bool(on)
            tmp = recdir / ".control.json.tmp"
            tmp.write_text(json.dumps(cur))
            tmp.replace(path)
            if demo:       # im Demo-Modus gibt es keinen Recorder: Status direkt nachziehen
                st_path = recdir / ".status.json"
                try:
                    st = json.loads(st_path.read_text())
                    for a in st.get("active", []):
                        if a["id"] == rid:
                            a["recording"] = bool(on)
                    st_path.write_text(json.dumps(st))
                except (FileNotFoundError, ValueError):
                    pass

    def active_calls() -> list[dict]:
        st = recorder_status()
        if not st.get("online"):
            return []
        try:
            want = json.loads((recdir / ".control.json").read_text() or "{}")
        except (FileNotFoundError, ValueError):
            want = {}
        out = []
        for a in st.get("active", []):
            a = dict(a)
            # gerade umgeschaltet, Recorder hat den Status noch nicht geschrieben
            if a["id"] in want and time.time() - st.get("time", 0) < 3:
                a["recording"] = bool(want[a["id"]])
            out.append(a)
        return out

    @app.post("/api/recording/active/<rid>")
    def rec_toggle(rid):
        """Body: {on: bool}"""
        if not RE_ID.match(rid):
            raise RecError("Ungültiges Gespräch.")
        if rid not in {a["id"] for a in recorder_status().get("active", [])}:
            raise RecError("Das Gespräch ist schon beendet.")
        on = bool((request.get_json(force=True) or {}).get("on"))
        set_control(rid, on)
        return jsonify(ok=True, on=on)

    # ---------------- Telefondienst: Aufnahme am Telefon ein-/ausschalten ----------------
    # Ueber den Telefon-Port (vvm.py, /ipphone/...). Das Telefon wird an seiner IP erkannt und sieht nur
    # Gespraeche, an denen eine seiner Rufnummern beteiligt ist.

    import vvm as _vvm
    PH = "/ipphone/rec"
    pcache = {"t": 0.0, "map": ({}, {})}

    def phone_numbers(ip: str) -> list[str]:
        if time.time() - pcache["t"] > 20:
            pcache["map"] = (_vvm.parse_pool_ips(router.show("show voice register pool all brief")),
                             _vvm.parse_pool_numbers(router.show("show running-config | section voice register")))
            pcache["t"] = time.time()
        ips, nums = pcache["map"]
        pool = ips.get(ip) or (next(iter(ips.values()), None) if demo else None)
        return nums.get(pool, []) if pool else []

    def tail(n: str) -> str:
        d = re.sub(r"\D", "", n or "")
        return d[-8:] if len(d) >= 8 else d

    def calls_for(ip: str) -> list[dict]:
        mine = {tail(n) for n in phone_numbers(ip) if tail(n)}
        return [a for a in active_calls() if mine & {tail(n) for n in a.get("numbers", [])}]

    def other_side(a: dict, ip: str) -> str:
        mine = {tail(n) for n in phone_numbers(ip)}
        rest = [n for n in a.get("numbers", []) if tail(n) not in mine]
        return rest[0] if rest else "?"

    def phone_page(note: str = ""):
        ip = request.remote_addr or ""
        base = request.host_url.rstrip("/") + PH
        back = request.host_url.rstrip("/") + _vvm.PREFIX
        calls = calls_for(ip)
        if not calls:
            return _vvm.xml_resp(_vvm.xml_text("Gesprächsaufnahme", note, "Kein Gespräch mit Aufzeichnung aktiv.",
                                               [("Aktual.", base), ("Zurück", back), ("Beenden", _vvm.EXIT)]))
        if len(calls) > 1:
            items = [(f"{'● läuft' if a['recording'] else '○ aus'}  {other_side(a, ip)}  ({_vvm.fmt_dur(int(time.time() - a['start']))})",
                      f"{base}/{a['id']}") for a in calls]
            return _vvm.xml_resp(_vvm.xml_menu("Gesprächsaufnahme", note or "Gespräch wählen", items,
                                               [("Auswahl", "SoftKey:Select"), ("Aktual.", base), ("Beenden", _vvm.EXIT)]))
        return call_page(calls[0], note)

    def call_page(a: dict, note: str = ""):
        ip = request.remote_addr or ""
        base = request.host_url.rstrip("/") + PH
        u = f"{base}/{a['id']}"
        text = (f"Gespräch mit {other_side(a, ip)}\nDauer: {_vvm.fmt_dur(int(time.time() - a['start']))}\n\n"
                + ("Die Aufnahme läuft." if a["recording"] else "Die Aufnahme ist ausgeschaltet."))
        keys = [("Stopp", f"{u}/off")] if a["recording"] else [("Start", f"{u}/on")]
        keys += [("Aktual.", u), ("Beenden", _vvm.EXIT)]
        return _vvm.xml_resp(_vvm.xml_text("Gesprächsaufnahme", note or ("● Aufnahme" if a["recording"] else "○ keine Aufnahme"),
                                           text, keys))

    def phone_guard():
        if request.environ.get("cme.phone") != "1":
            return Response("Nicht gefunden", status=404)
        return None

    @app.get(PH)
    def rec_phone():
        return phone_guard() or phone_page()

    @app.get(f"{PH}/<rid>")
    def rec_phone_call(rid):
        g = phone_guard()
        if g:
            return g
        a = next((x for x in calls_for(request.remote_addr or "") if x["id"] == rid), None) if RE_ID.match(rid) else None
        return call_page(a) if a else phone_page("Gespräch beendet")

    @app.get(f"{PH}/<rid>/<act>")
    def rec_phone_set(rid, act):
        g = phone_guard()
        if g:
            return g
        a = next((x for x in calls_for(request.remote_addr or "") if x["id"] == rid), None) if RE_ID.match(rid) else None
        if not a or act not in ("on", "off"):
            return phone_page("Gespräch beendet")
        set_control(rid, act == "on")
        a["recording"] = act == "on"
        return call_page(a, "Aufnahme eingeschaltet" if act == "on" else "Aufnahme ausgeschaltet")

    def phone_menu_item(ip: str):
        """Eintrag im Menue des Telefondienstes (vvm.py)."""
        if not ctx["enabled"] or not settings().get("enabled", True):
            return None
        calls = calls_for(ip)
        url = request.host_url.rstrip("/") + PH
        if calls:
            on = any(a["recording"] for a in calls)
            return (f"{'● Aufnahme läuft' if on else '○ Aufnahme aus'} – ein/aus", url, True)
        return ("Gesprächsaufnahme", url, False)

    app.config.setdefault("PHONE_MENU", []).append(phone_menu_item)

    # ---------------- Benutzerseite (portal.py) ----------------

    def rec_numbers(m: dict) -> set[str]:
        nums = {m.get("internal", ""), m.get("external", "")} | {p.get("number", "") for p in m.get("parties") or []}
        return {n for n in nums if n}

    def matches(m: dict, nums: list[str]) -> bool:
        """Gehoert die Aufnahme zu einer der Rufnummern? Kurze Nummern exakt, lange ueber die letzten 8 Ziffern."""
        mine = {n for n in nums if n}
        long_tails = {tail(n) for n in mine if len(re.sub(r"\D", "", n)) >= 8}
        for n in rec_numbers(m):
            if n in mine or (len(re.sub(r"\D", "", n)) >= 8 and tail(n) in long_tails):
                return True
        return False

    def user_recordings(nums: list[str], limit: int = 200) -> list[dict]:
        if not nums:
            return []
        keep = ("id", "start", "duration", "direction", "internal", "external", "parties", "channels", "partial",
                "transcript", "transcript_status")
        return [{k: m.get(k) for k in keep} for m in recordings(limit=5000) if matches(m, nums)][:limit]

    def user_may(rid: str, nums: list[str]) -> bool:
        if not RE_ID.match(rid):
            return False
        try:
            m = json.loads((recdir / f"{rid}.json").read_text())
        except (OSError, ValueError):
            return False
        return matches(m, nums)

    app.config["RECORDING"] = {"enabled": lambda: bool(ctx["enabled"]), "list": user_recordings, "may": user_may,
                               # fuer die Apps (phoneapp.py): laufende Gespraeche, Aufnahme ein/aus, Einstellungen
                               "active": active_calls, "set": set_control, "settings": settings}

    # Asterisk-Datei beim Start neu schreiben (z. B. nach einem Update)
    if ctx["enabled"]:
        try:
            save(settings(), refresh=False)
        except Exception:
            pass


def demo_seed(recdir: Path) -> None:
    """Zwei Beispielaufnahmen (Ton) fuer den Demo-Modus."""
    recdir.mkdir(parents=True, exist_ok=True)
    if any(recdir.glob("*.json")):
        return
    import math
    now = time.time()
    rows = [("in", "101", "+4917012345678", 95, "Thomas", "Mobil Anna",
             "[0:00] Thomas: Guten Tag, Wiederhold.\n[0:02] Mobil Anna: Hallo, hier ist Anna, ich wollte kurz "
             "wegen des Termins morgen anrufen.\n[0:07] Thomas: Ja gerne, passt zehn Uhr?"),
            ("out", "102", "06151123456", 42, "Küche", "06151123456", "")]
    for i, (direction, internal, external, dur, a, b, text) in enumerate(rows):
        start = now - 3600 * (i * 20 + 2)
        rid = time.strftime("%Y%m%d-%H%M%S", time.localtime(start)) + f"-{i:06x}"
        with wave.open(str(recdir / f"{rid}.wav"), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(8000)
            frames = bytearray()
            for n in range(8000 * 3):
                l = int(4000 * math.sin(2 * math.pi * 440 * n / 8000))
                r = int(4000 * math.sin(2 * math.pi * 660 * n / 8000))
                frames += l.to_bytes(2, "little", signed=True) + r.to_bytes(2, "little", signed=True)
            w.writeframes(bytes(frames))
        meta = {"id": rid, "start": start, "duration": dur, "direction": direction, "internal": internal,
                "external": external, "parties": [], "channels": [a, b], "codec": "G722", "file": f"{rid}.wav",
                "size": (recdir / f"{rid}.wav").stat().st_size, "transcript": text,
                "transcript_status": "ok" if text else "off", "mailed": "thomas@example.de" if text else "",
                "mail_error": ""}
        (recdir / f"{rid}.json").write_text(json.dumps(meta, ensure_ascii=False))
