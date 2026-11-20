"""
features.py - Rufuebernahme (Pickup-Gruppen), Berechtigungen (Class of Restriction) und Konferenzraeume

Rufuebernahme
  voice register dn N / pickup-group <gruppe>. Die Telefone der Mitglieder bekommen die Softkeys
  PickUp und GPickUp (voice register template); "no service directed-pickup" unter telephony-service
  macht PickUp zur Uebernahme in der eigenen Gruppe mit einem Tastendruck.

Berechtigungen (COR, feste Stufen)
  Je Telefon (voice register pool) eine Stufe: nur intern / Festnetz Inland / + Mobilfunk / + Ausland /
  unbeschraenkt (inkl. Sonderrufnummern). Dafuer:
    dial-peer cor custom           CME-INTERN, CME-NATIONAL, CME-MOBIL, CME-INTL, CME-SONDER
    dial-peer cor list CME-OUT-*   je Ziel-Kategorie (an den Dial-Peers zum Provider)
    dial-peer cor list CME-IN-*    je Stufe (am Telefon: cor incoming CME-IN-... default)
  IOS probiert bei einer COR-Sperre den naechsten passenden Dial-Peer - deshalb ist der Provider-Dial-Peer
  selbst (z. B. 0T) die strengste Kategorie "Sonderrufnummern" (alles, was nicht eindeutig zugeordnet ist:
  0900, 0137, 0180, 0700, Call-by-Call ...). Fuer Festnetz (0[2-6], 07[1-9], 08[1-9], 0800, 09[1-9]), Mobilfunk (015-017)
  und Ausland (00) legt cme-web Kopien mit engerem Muster an ("COR ... zu DP n (cme-web)"); die werden im
  Hintergrund nachgezogen, wenn sich der Provider-Dial-Peer aendert. Dial-Peers nur fuer Notrufe
  (110, 112, 115, 116...) bleiben ohne Sperre.

Autoregistrierung (SIP)
  voice register global / auto-register mit password, auto-assign <von> to <bis> und
  service-enable / no service-enable (ein/aus).

Konferenzraeume (Asterisk ConfBridge)
  cme/extensions_conf.conf   [cme-conf] mit den Nummern, je Raum [conf-<id>] (PIN-Abfrage)
  cme/confbridge_cme.conf    je Raum Bridge-, Teilnehmer- und Moderatorprofil
  Router: je Nummer ein Dial-Peer zu Asterisk ("Konferenz (Asterisk) ...").
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_NUM = re.compile(r"^\+?[0-9*#]{1,32}$")
RE_PIN = re.compile(r"^[0-9]{3,8}$")
RE_NAME = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{1,40}$")
CONF_DESC = "Konferenz (Asterisk)"

# ---------------- Rufuebernahme ----------------
PICKUP_KEYS = ["Pickup", "Gpickup"]
PICKUP_IDLE = "Redial Newcall Cfwdall Pickup Gpickup DND"

# ---------------- COR ----------------
COR_NAMES = ["CME-INTERN", "CME-NATIONAL", "CME-MOBIL", "CME-INTL", "CME-SONDER"]
STAGES = {                                   # Stufe -> Mitglieder der Liste am Telefon
    "intern": ["CME-INTERN"],
    "national": ["CME-NATIONAL"],
    "mobil": ["CME-NATIONAL", "CME-MOBIL"],
    "intl": ["CME-NATIONAL", "CME-MOBIL", "CME-INTL"],
}
STAGE_LABEL = {"": "unbeschränkt", "intern": "nur intern", "national": "Festnetz Inland",
               "mobil": "Inland + Mobilfunk", "intl": "Inland, Mobilfunk + Ausland"}
OUT_LISTS = {"national": "CME-NATIONAL", "mobil": "CME-MOBIL", "intl": "CME-INTL", "sonder": "CME-SONDER"}
# Ziel-Kategorien im nationalen Format (mit 0); alles andere ueber den Provider-Dial-Peer = Sonderrufnummer.
# 0900 (Mehrwertdienst) liegt nicht in 090[1-9] / 09[1-9]; Notrufe und 116xxx ohne Einschraenkung.
CATEGORIES = [("national", ["0[2-6]T", "07[1-9]T", "08[1-9]T", "0800T", "09[1-9]T", "[2-9]T"]),
              ("mobil", ["01[5-7]T"]), ("intl", ["00T"]),
              ("notruf", ["110", "112", "115", "116T"])]
STAGE_LISTS = {f"CME-IN-{s.upper()}" for s in STAGES}
HEAD = 1 + len(COR_NAMES) + 2 * len(OUT_LISTS) + sum(1 + len(m) for m in STAGES.values())
RE_EMERGENCY = re.compile(r"^(110|112|115|116T|116\.+|19222)$")
RE_CLONE = re.compile(r"^COR (\w+) (\S+) zu DP (\d+) \(cme-web\)$")
CLONE_SKIP = ("description ", "destination-pattern ", "incoming ", "answer-address ", "corlist ",
              "destination provision-policy ", "destination e164-pattern-map ")


class FeatError(ValueError):
    pass


def ios_regex(pattern: str) -> re.Pattern | None:
    """Dial-Peer-Muster -> Regex (fuer die Pruefung, ob eine Kategorie ueber den Provider laeuft)."""
    out, i = "^", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "[":
            j = pattern.find("]", i)
            if j < 0:
                return None
            out += pattern[i:j + 1]
            i = j
        elif c == ".":
            out += "[0-9*#]"
        elif c == "T":
            out += "[0-9*#]*"
        elif c in "+*#":
            out += "\\" + c
        elif c.isdigit():
            out += c
        else:
            return None
        i += 1
    try:
        return re.compile(out + "$")
    except re.error:
        return None


def sample(pattern: str) -> str:
    """Beispielnummer zu einem Muster: 01[5-7]T -> 0151234"""
    s = re.sub(r"\[(.)[^\]]*\]", r"\1", pattern)
    return s.replace("T", "1234").replace(".", "1")


def parse_voice_register(cfg: str) -> tuple[dict, dict, dict]:
    """-> (dns {tag: {number, name, pickup}}, pools {tag: {desc, type, dns, template, cor}}, templates {tag: {idle, lines}})"""
    dns, pools, temps, cur, kind = {}, {}, {}, None, ""
    for line in cfg.splitlines():
        m = re.match(r"^voice register (dn|pool|template)\s+(\d+)\s*$", line)
        if m:
            kind, tag = m.groups()
            if kind == "dn":
                cur = dns.setdefault(tag, {"tag": tag, "number": "", "name": "", "pickup": ""})
            elif kind == "pool":
                cur = pools.setdefault(tag, {"tag": tag, "desc": "", "type": "", "dns": [], "template": "", "cor": ""})
            else:
                cur = temps.setdefault(tag, {"tag": tag, "idle": None, "lines": []})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        if kind == "dn":
            if t.startswith("number "):
                cur["number"] = t.split()[1]
            elif t.startswith("name "):
                cur["name"] = t[5:].strip()
            elif t.startswith("pickup-group "):
                cur["pickup"] = t.split()[1]
        elif kind == "pool":
            mm = re.match(r"^number \d+ dn (\d+)", t)
            if mm:
                cur["dns"].append(mm.group(1))
            elif t.startswith("description "):
                cur["desc"] = t[12:]
            elif t.startswith("type "):
                cur["type"] = t[5:]
            elif t.startswith("template "):
                cur["template"] = t.split()[1]
            else:
                mm = re.match(r"^cor incoming (\S+)", t)
                if mm:
                    cur["cor"] = mm.group(1)
        else:
            cur["lines"].append(t)
            if t.startswith("softkeys idle"):
                cur["idle"] = t.split()[2:]
    return dns, pools, temps


def parse_peers(cfg: str) -> dict[str, dict]:
    peers, cur = {}, None
    for line in cfg.splitlines():
        m = re.match(r"^dial-peer voice (\d+) (\w+)", line)
        if m:
            cur = peers.setdefault(m.group(1), {"tag": m.group(1), "kind": m.group(2), "lines": [], "desc": "",
                                                "pattern": "", "target": "", "corlist": ""})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        cur["lines"].append(t)
        if t.startswith("description "):
            cur["desc"] = t[12:]
        elif t.startswith("destination-pattern "):
            cur["pattern"] = t.split()[1]
        elif t.startswith("session target "):
            cur["target"] = t.split()[2]
        elif t.startswith("session server-group "):
            cur["target"] = "server-group " + t.split()[2]
        elif t.startswith("corlist outgoing "):
            cur["corlist"] = t.split()[2]
    return peers


def parse_autoreg(cfg: str) -> dict:
    """voice register global / auto-register -> {present, enabled, password, start, end}"""
    res = {"present": False, "enabled": False, "password": False, "start": "", "end": "", "template": ""}
    glob = sub = False
    disabled = False
    for line in cfg.splitlines():
        if not line.startswith(" "):
            glob = line.strip() == "voice register global"
            sub = False
            continue
        if not glob:
            continue
        if re.match(r"^ \S", line):
            sub = line.strip() == "auto-register"
            if sub:
                res["present"] = True
            continue
        if not sub:
            continue
        t = line.strip()
        if t.startswith("password "):
            res["password"] = True
        elif t == "no service-enable":
            disabled = True
        else:
            m = re.match(r"^auto-assign (\d+) to (\d+)", t)
            if m:
                res["start"], res["end"] = m.groups()
            m = re.match(r"^template (\d+)", t)
            if m:
                res["template"] = m.group(1)
    res["enabled"] = res["present"] and not disabled
    return res


def register(app, router, apply, demo: bool, ctx: dict) -> None:
    cfgdir: Path = ctx["cfgdir"]
    host_ip = ctx["host_ip"]
    ami = ctx["ami"]
    conf_lock = threading.Lock()
    sync_state = {"error": "", "last": 0, "failed": {}}

    def show_vr() -> str:
        return router.show("show running-config | section voice register")

    def show_peers() -> str:
        return router.show("show running-config | section dial-peer voice")

    @app.errorhandler(FeatError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/features.js")
    def features_js():
        return send_file(BASE / "static" / "features.js", mimetype="text/javascript", max_age=0)

    def reply(res: dict):
        return jsonify(res), (200 if res.get("ok") else 400)

    # =====================================================================
    # Rufuebernahme
    # =====================================================================

    @app.get("/api/pickup")
    def pickup_get():
        dns, pools, temps = parse_voice_register(show_vr())
        ts = router.show("show running-config | section telephony-service")
        groups: dict[str, list[str]] = {}
        for d in dns.values():
            if d["pickup"]:
                groups.setdefault(d["pickup"], []).append(d["tag"])
        return jsonify(ok=True, dns=[{"tag": d["tag"], "number": d["number"], "name": d["name"], "group": d["pickup"],
                                      "pools": [p["tag"] for p in pools.values() if d["tag"] in p["dns"]]}
                                     for d in sorted(dns.values(), key=lambda x: int(x["tag"])) if d["number"]],
                       groups=[{"group": g, "dns": v} for g, v in sorted(groups.items(), key=lambda kv: kv[0])],
                       directed=not re.search(r"^\s*no service directed-pickup", ts, re.M))

    @app.put("/api/pickup")
    def pickup_put():
        """Body: {assign: {dn-tag: gruppe|""}, restart: bool}"""
        b = request.get_json(force=True) or {}
        dns, pools, temps = parse_voice_register(show_vr())
        want = {}
        for tag, g in (b.get("assign") or {}).items():
            tag, g = str(tag), str(g or "").strip()
            if tag not in dns:
                raise FeatError(f"DN {tag} gibt es nicht.")
            if g and not re.fullmatch(r"[0-9]{1,8}", g):
                raise FeatError(f"Gruppennummer {g}: nur Ziffern (1-8 Stellen).")
            if g and any(d["number"] == g for d in dns.values()):
                raise FeatError(f"Gruppennummer {g} ist schon eine Rufnummer – bitte eine andere wählen.")
            want[tag] = g
        lines, touched = [], set()
        for tag, g in want.items():
            if dns[tag]["pickup"] == g:
                continue
            lines.append(f"voice register dn {tag}")
            lines.append(f" pickup-group {g}" if g else f" no pickup-group {dns[tag]['pickup']}")
            touched |= {p["tag"] for p in pools.values() if tag in p["dns"]}
        members = {t for t, g in ({**{t: d["pickup"] for t, d in dns.items()}, **want}).items() if g}
        # Softkeys PickUp/GPickUp an den Telefonen der Mitglieder
        need = [p for p in pools.values() if set(p["dns"]) & members]
        pre, pool_lines = [], []
        own_tpl = next((t for t, x in temps.items() if x["idle"] == PICKUP_IDLE.split()
                        and len(x["lines"]) == 1), None)
        for p in need:
            t = p["template"]
            if t and t in temps:
                idle = temps[t]["idle"]
                if idle is None:
                    pre += [f"voice register template {t}", f" softkeys idle {PICKUP_IDLE}"]
                    temps[t]["idle"] = PICKUP_IDLE.split()
                    touched |= {q["tag"] for q in pools.values() if q["template"] == t}
                elif any(k not in idle for k in PICKUP_KEYS):
                    new = idle + [k for k in PICKUP_KEYS if k not in idle]
                    pre += [f"voice register template {t}", f" softkeys idle {' '.join(new)}"]
                    temps[t]["idle"] = new
                    touched |= {q["tag"] for q in pools.values() if q["template"] == t}
            elif not t:
                if own_tpl is None:
                    used = {int(x) for x in temps}
                    free = [i for i in range(1, 11) if i not in used]
                    if not free:
                        raise FeatError("Es gibt schon 10 Templates (Höchstzahl) – keins frei für die Übernahme-Tasten.")
                    own_tpl = str(free[0])
                    pre += [f"voice register template {own_tpl}", f" softkeys idle {PICKUP_IDLE}"]
                    temps[own_tpl] = {"tag": own_tpl, "idle": PICKUP_IDLE.split(), "lines": [f"softkeys idle {PICKUP_IDLE}"]}
                pool_lines += [f"voice register pool {p['tag']}", f" template {own_tpl}"]
                touched.add(p["tag"])
        ts = router.show("show running-config | section telephony-service")
        if members and not re.search(r"^\s*no service directed-pickup", ts, re.M):
            pre += ["telephony-service", " no service directed-pickup"]
        lines = pre + lines + pool_lines
        if not lines:
            return jsonify(ok=True, config=[], unchanged=True)
        lines += ["voice register global", " create profile"]
        if b.get("restart", True):
            for t in sorted(touched, key=int):
                lines += [f"voice register pool {t}", " restart"]
        res = apply(lines)
        if res.get("ok"):
            now, _, _ = parse_voice_register(show_vr())
            bad = [now[t]["number"] for t, g in want.items() if t in now and now[t]["pickup"] != g]
            if bad:
                res.update(ok=False, error="Der Router hat die Gruppe nicht übernommen für: " + ", ".join(bad))
        res["restarted"] = sorted(touched, key=int) if b.get("restart", True) else []
        return reply(res)

    # =====================================================================
    # Berechtigungen (COR)
    # =====================================================================

    def is_provider(p: dict) -> bool:
        if p["kind"] != "voip" or not p["target"]:
            return False
        if host_ip and host_ip in p["target"]:
            return False
        return not re.search(r"Asterisk|cme-recorder|cme-web", p["desc"])

    def access_of(pattern: str) -> str | None:
        if pattern in ("T", ".T"):
            return ""
        if pattern.endswith("0T"):
            return pattern[:-2]
        m = re.fullmatch(r"(\d+)T", pattern)
        return m.group(1) if m else None

    def desired_clones(peers: dict) -> tuple[dict, list[dict]]:
        """-> ({(peer, muster): {cat, lines}}, Provider-Infos)"""
        want, info = {}, []
        for p in peers.values():
            if not is_provider(p) or not p["pattern"]:
                continue                       # nur abgehende Provider-Dial-Peers
            if RE_EMERGENCY.match(p["pattern"]):
                info.append({"tag": p["tag"], "desc": p["desc"], "pattern": p["pattern"], "supported": True,
                             "corlist": p["corlist"], "clones": [], "emergency": True})
                continue
            acc = access_of(p["pattern"])
            rx = ios_regex(p["pattern"]) if p["pattern"] else None
            cats = []
            if acc is not None and rx is not None:
                base = [l for l in p["lines"] if not l.startswith(CLONE_SKIP)]
                for cat, pats in CATEGORIES:
                    for pat in pats:
                        full = acc + pat
                        if not rx.match(sample(full)):
                            continue        # laeuft gar nicht ueber diesen Provider
                        lines = [f"description COR {cat} {full} zu DP {p['tag']} (cme-web)",
                                 f"destination-pattern {full}"] + base
                        if cat in OUT_LISTS:
                            lines.append(f"corlist outgoing CME-OUT-{cat.upper()}")
                        want[(p["tag"], full)] = {"cat": cat, "lines": lines}
                        cats.append(full)
            info.append({"tag": p["tag"], "desc": p["desc"], "pattern": p["pattern"], "supported": acc is not None,
                         "corlist": p["corlist"], "clones": cats})
        return want, info

    def existing_clones(peers: dict) -> dict:
        out = {}
        for p in peers.values():
            m = RE_CLONE.match(p["desc"])
            if m:
                out[(m.group(3), m.group(2))] = p
        return out

    def cor_active(pools: dict) -> bool:
        # nur die Sperrstufen zaehlen; CME-IN-VIA-* gehoert zum Absender-Routing (e164.py)
        return any(p["cor"] in STAGE_LISTS for p in pools.values())

    def cor_lines(active: bool, peers: dict) -> list[str]:
        """Befehle, damit Klassen, Listen, Provider-Dial-Peers und Kopien zum Wunsch passen."""
        lines = []
        want, _ = desired_clones(peers) if active else ({}, [])
        have = existing_clones(peers)
        used = {int(t) for t in peers}
        if active:
            lines += ["dial-peer cor custom"] + [f" name {n}" for n in COR_NAMES]
            for cat, member in OUT_LISTS.items():
                lines += [f"dial-peer cor list CME-OUT-{cat.upper()}", f" member {member}"]
            for st, members in STAGES.items():
                lines += [f"dial-peer cor list CME-IN-{st.upper()}"] + [f" member {m}" for m in members]
        # Provider-Dial-Peers: alles nicht eindeutig Zugeordnete = Sonderrufnummern
        for p in peers.values():
            if not is_provider(p) or not p["pattern"] or RE_EMERGENCY.match(p["pattern"]):
                continue
            target = "CME-OUT-SONDER" if active and access_of(p["pattern"] or "") is not None else ""
            if p["corlist"].startswith("CME-OUT-VIA-"):
                continue                     # Absender-Routing, nicht anfassen
            if p["corlist"] != target and (target or p["corlist"].startswith("CME-OUT-")):
                lines += [f"dial-peer voice {p['tag']} voip",
                          f" corlist outgoing {target}" if target else f" no corlist outgoing {p['corlist']}"]
        # Kopien: ueberzaehlige weg, fehlende/abweichende neu
        for key, p in have.items():
            if key not in want:
                lines.append(f"no dial-peer voice {p['tag']} voip")
        for key, w in want.items():
            cur = have.get(key)
            if cur and sorted(cur["lines"]) == sorted(w["lines"]):
                continue
            if cur:
                tag = cur["tag"]
                lines.append(f"no dial-peer voice {tag} voip")
            else:
                tag = str(next(t for t in range(9500, 10000) if t not in used))
                used.add(int(tag))
            lines += [f"dial-peer voice {tag} voip"] + [f" {l}" for l in w["lines"]]
        return lines

    @app.get("/api/cor")
    def cor_get():
        dns, pools, _ = parse_voice_register(show_vr())
        peers = parse_peers(show_peers())
        active = cor_active(pools)
        _, info = desired_clones(peers)
        have = existing_clones(peers)
        stage_of = {f"CME-IN-{s.upper()}": s for s in STAGES}
        return jsonify(ok=True, active=active, stages=STAGE_LABEL,
                       pools=[{"tag": p["tag"], "desc": p["desc"], "type": p["type"],
                               "numbers": [dns[d]["number"] for d in p["dns"] if d in dns],
                               "stage": stage_of.get(p["cor"], ""), "foreign": bool(p["cor"]) and p["cor"] not in stage_of,
                               "egress": p["cor"].startswith("CME-IN-VIA-")}
                              for p in sorted(pools.values(), key=lambda x: int(x["tag"]))],
                       providers=info, clones=[{"tag": c["tag"], "desc": c["desc"], "pattern": c["pattern"]}
                                               for c in have.values()],
                       pending=bool(active and cor_lines(True, peers)[HEAD:]),
                       sync=sync_state)

    @app.put("/api/cor")
    def cor_put():
        """Body: {stages: {pool-tag: stufe|""}}"""
        b = request.get_json(force=True) or {}
        dns, pools, _ = parse_voice_register(show_vr())
        peers = parse_peers(show_peers())
        new = {t: p["cor"] for t, p in pools.items()}
        for tag, st in (b.get("stages") or {}).items():
            tag, st = str(tag), str(st or "")
            if tag not in pools:
                raise FeatError(f"Pool {tag} gibt es nicht.")
            if st and st not in STAGES:
                raise FeatError(f"Unbekannte Stufe: {st}")
            cur = pools[tag]["cor"]
            if cur.startswith("CME-IN-VIA-") and st:
                raise FeatError(f"Pool {tag} ist im Absender-Routing einem Provider zugeordnet ({cur}). Sperrstufen und "
                                "Absender-Routing lassen sich derzeit nicht gleichzeitig verwenden.")
            if cur and cur not in STAGE_LISTS and not cur.startswith("CME-IN-VIA-"):
                raise FeatError(f"Pool {tag} hat schon eine eigene COR-Liste ({cur}) – bitte am Router entfernen.")
            if cur.startswith("CME-IN-VIA-"):
                continue
            new[tag] = f"CME-IN-{st.upper()}" if st else ""
        active = any(v in STAGE_LISTS for v in new.values())
        if active and any(v.startswith("CME-IN-VIA-") for v in new.values()):
            raise FeatError("Im Absender-Routing sind Telefone einem Provider zugeordnet. Sperrstufen und "
                            "Absender-Routing lassen sich derzeit nicht gleichzeitig verwenden.")
        lines = cor_lines(active, peers) if active else []
        for tag, v in new.items():
            cur = pools[tag]["cor"]
            if cur == v:
                continue
            lines.append(f"voice register pool {tag}")
            if cur:
                lines.append(f" no cor incoming {cur} default")
            if v:
                lines.append(f" cor incoming {v} default")
        if not active:
            lines += cor_lines(False, peers)
        if not lines:
            return jsonify(ok=True, config=[], unchanged=True)
        res = apply(lines)
        if res.get("ok"):
            _, now, _ = parse_voice_register(show_vr())
            bad = [t for t, v in new.items() if t in now and now[t]["cor"] != v]
            if bad:
                res.update(ok=False, error="Der Router hat die Stufe nicht übernommen für Pool " + ", ".join(bad)
                           + ".\n\n" + "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-20:]))
        sync_state["failed"] = {}
        return reply(res)

    def cor_sync_loop():
        """Kopien der Provider-Dial-Peers nachziehen, wenn sich die Provider-Dial-Peers aendern."""
        time.sleep(30)
        while True:
            try:
                _, pools, _ = parse_voice_register(show_vr())
                if cor_active(pools):
                    peers = parse_peers(show_peers())
                    lines = [l for l in cor_lines(True, peers)]
                    # Klassen und Listen werden immer mitgeschickt - nur handeln, wenn sich mehr aendert
                    head = HEAD
                    todo = lines[head:]
                    key = "\n".join(todo)
                    if todo and sync_state["failed"].get(key, 0) < 2:
                        res = apply(lines)
                        sync_state["last"] = int(time.time())
                        if res.get("ok"):
                            again = cor_lines(True, parse_peers(show_peers()))[head:]
                            if again:
                                sync_state["failed"][key] = sync_state["failed"].get(key, 0) + 1
                                sync_state["error"] = "Kopien der Provider-Dial-Peers ließen sich nicht abgleichen."
                            else:
                                sync_state["error"] = ""
                        else:
                            sync_state["failed"][key] = sync_state["failed"].get(key, 0) + 1
                            sync_state["error"] = res.get("error") or "Fehler beim Abgleich"
            except Exception as e:
                sync_state["error"] = f"{type(e).__name__}: {e}"
            time.sleep(60)

    if not demo:
        threading.Thread(target=cor_sync_loop, daemon=True, name="cor-sync").start()

    # =====================================================================
    # Autoregistrierung (SIP-Telefone melden sich ohne vorher angelegten Pool an)
    # =====================================================================

    def autoreg_state() -> dict:
        st = parse_autoreg(router.show("show running-config | section voice register global"))
        dns, pools, _ = parse_voice_register(show_vr())
        auto = [p for p in pools.values() if p["desc"] == "AUTO-REG"]
        st["pools"] = [{"tag": p["tag"], "type": p["type"],
                        "numbers": [dns[d]["number"] for d in p["dns"] if d in dns]} for p in auto]
        return st

    @app.get("/api/autoreg")
    def autoreg_get():
        return jsonify(ok=True, **autoreg_state())

    @app.put("/api/autoreg")
    def autoreg_put():
        """Body: {enabled: bool, password: neu|"", start, end}"""
        b = request.get_json(force=True) or {}
        cur = autoreg_state()
        enabled = bool(b.get("enabled"))
        pw = str(b.get("password") or "").strip()
        start, end = str(b.get("start") or "").strip(), str(b.get("end") or "").strip()
        if pw and not re.fullmatch(r"[A-Za-z0-9]{4,32}", pw):
            raise FeatError("Passwort: 4–32 Buchstaben/Ziffern ohne Sonderzeichen.")
        if start or end:
            if not (start.isdigit() and end.isdigit()):
                raise FeatError("Rufnummernbereich: von und bis als Ziffern angeben.")
            if len(start) != len(end) or int(start) > int(end):
                raise FeatError("Rufnummernbereich: „von“ muss kleiner als „bis“ sein und gleich viele Stellen haben.")
            if int(end) - int(start) > 999:
                raise FeatError("Rufnummernbereich: höchstens 1000 Nummern.")
        if enabled:
            if not (pw or cur["password"]):
                raise FeatError("Zum Einschalten bitte ein Passwort festlegen.")
            if not ((start and end) or cur["start"]):
                raise FeatError("Zum Einschalten bitte den Rufnummernbereich angeben.")
        sub = []
        if enabled != cur["enabled"]:
            sub.append("  service-enable" if enabled else "  no service-enable")
        if pw:
            sub.append(f"  password {pw}")
        if start and end and (start, end) != (cur["start"], cur["end"]):
            sub.append(f"  auto-assign {start} to {end}")
        if not sub:
            return jsonify(ok=True, config=[], unchanged=True)
        res = apply(["voice register global", " auto-register"] + sub)
        if res.get("ok"):
            now = autoreg_state()
            if now["enabled"] != enabled or (start and end and (now["start"], now["end"]) != (start, end)):
                res.update(ok=False, error="Der Router hat die Einstellung nicht übernommen.\n\n"
                           + "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-20:]))
        return reply(res)

    # =====================================================================
    # Konferenzraeume (Asterisk ConfBridge)
    # =====================================================================

    store = cfgdir / "cme-conf.json"

    def data() -> dict:
        try:
            d = json.loads(store.read_text())
        except (FileNotFoundError, ValueError):
            d = {}
        d.setdefault("rooms", [])
        return d

    def music() -> list[dict]:
        try:
            return [{"id": m["id"], "name": m["name"]}
                    for m in json.loads((cfgdir / "cme-queues.json").read_text()).get("music", [])]
        except (FileNotFoundError, ValueError, KeyError):
            return []

    def profiles(r: dict) -> str:
        i = r["id"]
        moh = [f"music_on_hold_when_empty=yes", f"music_on_hold_class=cme-{r['music']}"] if r.get("music") \
            else ["music_on_hold_when_empty=no"]
        ann = ["announce_user_count=yes", "announce_only_user=yes"] if r.get("announce", True) \
            else ["quiet=yes"]
        out = [f"; {r['name']}", f"[cme-{i}-bridge]", "type=bridge"]
        if r.get("max"):
            out.append(f"max_members={int(r['max'])}")
        out += ["", f"[cme-{i}-user]", "type=user", "dsp_drop_silence=yes", *moh, *ann]
        if r.get("mpin"):
            out += ["wait_marked=yes", "end_marked=yes"]
        out += ["", f"[cme-{i}-admin]", "type=user", "admin=yes", "marked=yes", "dsp_drop_silence=yes", *moh, *ann, ""]
        return "\n".join(out)

    def dialplan(d: dict) -> str:
        out = ["; Von cme-web erzeugt - Aenderungen hier gehen verloren.", "", "[cme-conf]"]
        for r in d["rooms"]:
            for num in (r["number"], r.get("external", "")):
                if num:
                    out.append(f"exten => {num},1,Goto(conf-{r['id']},s,1)")
        for r in d["rooms"]:
            i = r["id"]
            user = f"ConfBridge(cme-{i},cme-{i}-bridge,cme-{i}-user,cme-user-menu)"
            admin = f"ConfBridge(cme-{i},cme-{i}-bridge,cme-{i}-admin,cme-admin-menu)"
            out += ["", f"; {r['name']}", f"[conf-{i}]",
                    "exten => s,1,Answer()",
                    " same => n,Wait(0.5)"]
            if not r.get("pin") and not r.get("mpin"):
                out += [f" same => n,{user}", " same => n,Hangup()"]
                continue
            out += [" same => n,Set(CTRY=0)",
                    " same => n(pin),Read(CPIN,conf-getpin,8,,1,10)"]
            if r.get("mpin"):
                out.append(f' same => n,GotoIf($["${{CPIN}}" = "{r["mpin"]}"]?admin)')
            out.append(f' same => n,GotoIf($["${{CPIN}}" = "{r.get("pin", "")}"]?user)')
            out += [" same => n,Set(CTRY=$[${CTRY} + 1])",
                    " same => n,Playback(conf-invalidpin)",
                    " same => n,GotoIf($[${CTRY} < 3]?pin)",
                    " same => n,Playback(vm-goodbye)",
                    " same => n,Hangup()",
                    f" same => n(user),{user}",
                    " same => n,Hangup()",
                    f" same => n(admin),{admin}",
                    " same => n,Hangup()"]
        return "\n".join(out) + "\n"

    def save(d: dict) -> None:
        cfgdir.mkdir(parents=True, exist_ok=True)
        files = {"cme-conf.json": json.dumps(d, indent=2, ensure_ascii=False),
                 "extensions_conf.conf": dialplan(d),
                 "confbridge_cme.conf": "; Von cme-web erzeugt\n\n" + "\n".join(profiles(r) for r in d["rooms"])}
        for name, text in files.items():
            tmp = cfgdir / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(cfgdir / name)
        if not demo and ami:
            for cmd in ("module reload app_confbridge.so", "dialplan reload"):
                try:
                    ami.command(cmd)
                except Exception:
                    pass

    def ascii_desc(text: str) -> str:
        for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
            text = text.replace(a, b)
        return re.sub(r"[^A-Za-z0-9 _.,()/+\-]", "", text).strip()[:60]

    def routed(d: dict) -> dict[str, str]:
        out = {}
        for r in d["rooms"]:
            for num in (r["number"], r.get("external", "")):
                if num:
                    out[num] = ascii_desc(f"{CONF_DESC} {r['name']}")
        return out

    def asterisk_peers() -> list[dict]:
        out = []
        for p in parse_peers(show_peers()).values():
            m = re.match(r"ipv4:([\d.]+)", p["target"])
            out.append({**p, "ip": m.group(1) if m else ""})
        return out

    def sync_router(old: dict[str, str], new: dict[str, str]) -> dict:
        existing = asterisk_peers()
        used = {int(p["tag"]) for p in existing}
        lines = []
        for num in old:
            if num not in new:
                p = next((x for x in existing if x["pattern"] == num and x["ip"] == host_ip
                          and x["desc"].startswith(CONF_DESC)), None)
                if p:
                    lines.append(f"no dial-peer voice {p['tag']} voip")
        for num, desc in new.items():
            p = next((x for x in existing if x["pattern"] == num and x["ip"] == host_ip), None)
            if p and old.get(num) == desc:
                continue
            tag = p["tag"] if p else str(next(t for t in range(8100, 9000) if t not in used))
            used.add(int(tag))
            lines += [f"dial-peer voice {tag} voip", f" description {desc}", f" destination-pattern {num}",
                      " session protocol sipv2", f" session target ipv4:{host_ip}:5060", " dtmf-relay rtp-nte",
                      ctx["codec_line"](), " no vad"]
        return apply(lines) if lines else {"ok": True, "error": None, "config": []}

    def taken_numbers(d: dict, own_id) -> dict[str, str]:
        taken = {}
        try:
            apps = json.loads((cfgdir / "cme-apps.json").read_text())
            taken.update({f["number"]: f"Fax {f['number']}" for f in apps.get("fax", [])})
            taken.update({i["number"]: f"Sprachmenü {i['name']}" for i in apps.get("ivrs", []) if i.get("number")})
        except (FileNotFoundError, ValueError):
            pass
        try:
            qs = json.loads((cfgdir / "cme-queues.json").read_text()).get("queues", [])
            taken.update({q["number"]: f"Warteschlange {q['name']}" for q in qs if q.get("number")})
        except (FileNotFoundError, ValueError):
            pass
        for r in d["rooms"]:
            if r["id"] != own_id:
                for num in (r["number"], r.get("external", "")):
                    if num:
                        taken[num] = f"Konferenz {r['name']}"
        for n in ctx["numbers"]():
            taken[n["number"]] = f"Rufnummer {n['name'] or n['number']}"
        taken[ctx["settings"]()["pilot"]] = "Voicemail-Nummer"
        return taken

    def room_input(b: dict, d: dict, own_id) -> dict:
        name = str(b.get("name") or "").strip()
        if not RE_NAME.match(name):
            raise FeatError("Name fehlt oder enthält unzulässige Zeichen.")
        number = str(b.get("number") or "").strip()
        if not RE_NUM.match(number):
            raise FeatError("Interne Nummer fehlt oder ist ungültig.")
        external = str(b.get("external") or "").strip()
        if external and not RE_NUM.match(external):
            raise FeatError("Externe Rufnummer ist ungültig.")
        if external == number:
            external = ""
        taken = taken_numbers(d, own_id)
        for num in (number, external):
            if num and num in taken:
                raise FeatError(f"{num} ist schon vergeben ({taken[num]}).")
        pin, mpin = str(b.get("pin") or "").strip(), str(b.get("mpin") or "").strip()
        for p, what in ((pin, "PIN"), (mpin, "Moderator-PIN")):
            if p and not RE_PIN.match(p):
                raise FeatError(f"{what}: 3–8 Ziffern.")
        if pin and pin == mpin:
            raise FeatError("PIN und Moderator-PIN müssen verschieden sein.")
        mx = str(b.get("max") or "").strip()
        if mx and (not mx.isdigit() or not 2 <= int(mx) <= 50):
            raise FeatError("Höchstens 2–50 Teilnehmer (oder leer lassen).")
        mus = str(b.get("music") or "")
        if mus and mus not in {m["id"] for m in music()}:
            raise FeatError("Unbekannte Wartemusik.")
        return {"name": name, "number": number, "external": external, "pin": pin, "mpin": mpin,
                "max": int(mx) if mx else 0, "music": mus, "announce": bool(b.get("announce", True))}

    def need():
        if not ctx["enabled"]:
            raise FeatError("Asterisk ist nicht eingerichtet (VM_HOST_IP, VM_AMI_SECRET).")

    def live() -> dict[str, int]:
        """Teilnehmer je Raum (confbridge list)."""
        if demo or not ami:
            return {}
        try:
            out = ami.command("confbridge list")
        except Exception:
            return {}
        res = {}
        for line in out.splitlines():
            m = re.match(r"^(cme-\d+)\s+(\d+)", line.strip())
            if m:
                res[m.group(1)] = int(m.group(2))
        return res

    @app.get("/api/conf")
    def conf_get():
        if not ctx["enabled"]:
            return jsonify(ok=True, enabled=False)
        d = data()
        try:
            ps = {p["pattern"]: p["tag"] for p in asterisk_peers() if p["ip"] == host_ip}
        except Exception:
            ps = {}
        act = live()
        rooms = [{**r, "dialpeer": ps.get(r["number"], ""), "dialpeer_ext": ps.get(r.get("external", ""), ""),
                  "active": act.get(f"cme-{r['id']}", 0)} for r in d["rooms"]]
        return jsonify(ok=True, enabled=True, rooms=rooms, music=music())

    @app.post("/api/conf")
    def conf_add():
        need()
        with conf_lock:
            d = data()
            old = routed(d)
            r = room_input(request.get_json(force=True) or {}, d, None)
            r["id"] = max([x["id"] for x in d["rooms"]] + [0]) + 1
            d["rooms"].append(r)
            res = sync_router(old, routed(d))
            if res.get("ok"):
                save(d)
            res["id"] = r["id"]
        return reply(res)

    @app.put("/api/conf/<int:rid>")
    def conf_edit(rid):
        need()
        with conf_lock:
            d = data()
            old = routed(d)
            i = next((k for k, x in enumerate(d["rooms"]) if x["id"] == rid), None)
            if i is None:
                raise FeatError("Raum gibt es nicht.")
            r = room_input(request.get_json(force=True) or {}, d, rid)
            r["id"] = rid
            d["rooms"][i] = r
            res = sync_router(old, routed(d))
            if res.get("ok"):
                save(d)
        return reply(res)

    @app.delete("/api/conf/<int:rid>")
    def conf_delete(rid):
        need()
        with conf_lock:
            d = data()
            old = routed(d)
            d["rooms"] = [x for x in d["rooms"] if x["id"] != rid]
            res = sync_router(old, routed(d))
            if res.get("ok"):
                save(d)
        return reply(res)

    # Sprachpakete fuer die Telefone (locales.py)
    import locales
    locales.register(app, router, apply, demo)
