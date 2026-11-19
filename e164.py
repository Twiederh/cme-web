"""
e164.py - E.164-Pattern-Maps (voice class e164-pattern-map) fuer cme_web

Anzeigen, neu anlegen, loeschen, Eintraege hinzufuegen/entfernen, Beschreibung aendern und
zeigen, welche Dial-Peers eine Map verwenden (destination / incoming called / incoming calling).
Wird aus translation.register() eingehaengt; die Oberflaeche ist static/e164.js (Tab Uebersetzungen).
"""

from __future__ import annotations

import re
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_PATTERN = re.compile(r"^[0-9A-D*#+.T\[\]\-^%?$,]{1,64}$")
MAX_TAG = 10000


class E164Error(ValueError):
    pass


def parse_maps(out: str) -> dict[str, dict]:
    maps, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^voice class e164-pattern-map\s+(\d+)", line)
        if m:
            cur = maps.setdefault(m.group(1), {"tag": m.group(1), "description": "", "entries": [], "url": ""})
            continue
        if cur is None or not line.startswith(" "):
            cur = None
            continue
        t = line.strip()
        if t.startswith("description "):
            cur["description"] = t[12:]
        elif t.startswith("e164 "):
            cur["entries"].append(t[5:].strip())
        elif t.startswith("url "):
            cur["url"] = t[4:].strip()
    return maps


def parse_usage(out: str) -> dict[str, list[dict]]:
    """show running-config | include ^dial-peer voice|e164-pattern-map -> {map: [{peer, kind, desc}]}"""
    used: dict[str, list[dict]] = {}
    peer = None
    for line in out.splitlines():
        m = re.match(r"^dial-peer voice\s+(\d+)", line)
        if m:
            peer = m.group(1)
            continue
        mm = re.match(r"^\s+(destination calling|destination|incoming called|incoming calling)\s+e164-pattern-map\s+(\d+)",
                      line)
        if mm and peer:
            used.setdefault(mm.group(2), []).append({"peer": peer, "kind": mm.group(1)})
    return used


def ascii_text(text: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^A-Za-z0-9 _.,:;()/+\-#*]", "", text).strip()[:80]


def entries_input(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    out = []
    for p in raw or []:
        p = str(p).strip()
        if not p:
            continue
        if not RE_PATTERN.match(p):
            raise E164Error(f"Ungültiges Muster: {p} (erlaubt: Ziffern, + . T [ ] - ^ % ? $ * #)")
        if p not in out:
            out.append(p)
    return out


EGRESS_DESC = "Absender fuer Dial-Peer {tag} (cme-web)"
DEFAULT_PATTERN = "T"         # jeder Absender, auch weitergeleitete externe Anrufer
# Regel fuer den eingehenden Dial-Peer: erst Absender UND Ziel passend, sonst wie bisher nur nach Ziel
POLICY_LINES = ["preference 1 calling called", "preference 2 called"]


def parse_peers(out: str) -> list[dict]:
    """Dial-Peers mit den Feldern fuer das Absender-Routing."""
    res, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^dial-peer voice (\d+) (\w+)", line)
        if m:
            cur = {"tag": m.group(1), "kind": m.group(2), "desc": "", "pattern": "", "target": "",
                   "calling_map": "", "policy": "", "dest_map": "", "incoming": ""}
            res.append(cur)
            continue
        if cur is None or not line.startswith(" "):
            cur = None
            continue
        t = line.strip()
        if t.startswith("description "):
            cur["desc"] = t[12:]
        elif t.startswith("destination-pattern "):
            cur["pattern"] = t.split()[1]
        elif t.startswith("session target "):
            cur["target"] = t.split()[2]
        elif t.startswith("session server-group "):
            cur["target"] = "server-group " + t.split()[2]
        elif t.startswith("destination calling e164-pattern-map "):
            cur["calling_map"] = t.split()[3]
        elif t.startswith("destination e164-pattern-map "):
            cur["dest_map"] = t.split()[2]
        elif t.startswith("destination provision-policy "):
            cur["policy"] = t.split()[2]
        elif t.startswith(("incoming ", "answer-address ")) and not cur["incoming"]:
            cur["incoming"] = t
    return res


def parse_policies(out: str) -> dict[str, list[str]]:
    pol, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^voice class dial-peer provision-policy\s+(\d+)", line)
        if m:
            cur = pol.setdefault(m.group(1), [])
            continue
        if cur is not None and line.startswith(" "):
            cur.append(line.strip())
        else:
            cur = None
    return pol


def parse_dns(out: str) -> list[dict]:
    dns, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^voice register dn\s+(\d+)", line)
        if m:
            cur = {"dn": m.group(1), "number": "", "name": ""}
            dns.append(cur)
            continue
        if cur is None or not line.startswith(" "):
            cur = None
            continue
        t = line.strip()
        if t.startswith("number "):
            cur["number"] = t.split()[1]
        elif t.startswith(("name ", "label ")) and not cur["name"]:
            cur["name"] = t.split(None, 1)[1]
    return [d for d in dns if d["number"]]


def register(app, router, apply) -> None:
    demo = type(router).__name__ == "DemoRouter"
    demo_state = {"maps": {"1": {"tag": "1", "description": "Eigene Rufnummern", "url": "",
                                 "entries": ["+4930123456..", "030123456.."]},
                           "2": {"tag": "2", "description": "Notrufe", "url": "", "entries": ["110", "112"]}},
                  "usage": "dial-peer voice 100 voip\n incoming called e164-pattern-map 1\n"
                           "dial-peer voice 200 voip\n",
                  "peers": {"100": {"desc": "Trunk sipgate", "pattern": "0T", "target": "sip-server"},
                            "300": {"desc": "Trunk Deutsche Giganetz", "pattern": "0T",
                                    "target": "ipv4:10.0.0.5:5060"}},
                  "extra": {}, "policies": {}}
    host_ip = __import__("os").environ.get("VM_HOST_IP", "")

    def show_maps() -> str:
        if demo:
            out = []
            for t, m in sorted(demo_state["maps"].items(), key=lambda kv: int(kv[0])):
                out.append(f"voice class e164-pattern-map {t}")
                if m["description"]:
                    out.append(f" description {m['description']}")
                out += [f" e164 {e}" for e in m["entries"]]
            return "\n".join(out)
        return router.show("show running-config | section voice class e164-pattern-map")

    def show_usage() -> str:
        if demo:
            out = []
            for t, p in demo_state["peers"].items():
                out.append(f"dial-peer voice {t} voip")
                if t == "100":
                    out.append(" incoming called e164-pattern-map 1")
                x = demo_state["extra"].get(t, {})
                if x.get("calling_map"):
                    out.append(f" destination calling e164-pattern-map {x['calling_map']}")
            return "\n".join(out)
        return router.show("show running-config | include ^dial-peer voice|e164-pattern-map")

    def show_peers() -> str:
        if demo:
            out = []
            for t, p in demo_state["peers"].items():
                out += [f"dial-peer voice {t} voip", f" description {p['desc']}",
                        f" destination-pattern {p['pattern']}", f" session target {p['target']}"]
                if t == "100":
                    out.append(" incoming called e164-pattern-map 1")
                x = demo_state["extra"].get(t, {})
                if x.get("calling_map"):
                    out.append(f" destination calling e164-pattern-map {x['calling_map']}")
                if x.get("policy"):
                    out.append(f" destination provision-policy {x['policy']}")
            return "\n".join(out)
        return router.show("show running-config | section dial-peer voice")

    def show_policies() -> str:
        if demo:
            return "\n".join(f"voice class dial-peer provision-policy {t}\n" + "\n".join(f" {l}" for l in ls)
                             for t, ls in demo_state["policies"].items())
        return router.show("show running-config | section provision-policy")

    def show_dns() -> str:
        if demo:
            return "\n".join(router._blocks("voice register dn"))
        return router.show("show running-config | section voice register dn")

    def do_apply(lines: list[str]) -> dict:
        res = apply(lines)
        if demo and res.get("ok"):
            cur = None
            peer = None
            for line in lines:
                mp = re.match(r"^dial-peer voice (\d+) voip$", line)
                mpol = re.match(r"^(no )?voice class dial-peer provision-policy (\d+)$", line)
                if mp:
                    peer, cur = demo_state["extra"].setdefault(mp.group(1), {}), None
                    continue
                if mpol:
                    peer, cur = None, None
                    if mpol.group(1):
                        demo_state["policies"].pop(mpol.group(2), None)
                    else:
                        demo_state["policies"][mpol.group(2)] = list(POLICY_LINES)
                    continue
                if peer is not None and line.startswith(" "):
                    t = line.strip()
                    for key, cmd in (("calling_map", "destination calling e164-pattern-map"),
                                     ("policy", "destination provision-policy")):
                        if t.startswith(cmd + " "):
                            peer[key] = t.split()[-1]
                        elif t.startswith("no " + cmd):
                            peer[key] = ""
                    continue
                peer = None
                m = re.match(r"^(no )?voice class e164-pattern-map (\d+)$", line)
                if m:
                    if m.group(1):
                        demo_state["maps"].pop(m.group(2), None)
                        cur = None
                    else:
                        cur = demo_state["maps"].setdefault(m.group(2), {"tag": m.group(2), "description": "",
                                                                          "entries": [], "url": ""})
                    continue
                if cur is None:
                    continue
                t = line.strip()
                if t.startswith("description "):
                    cur["description"] = t[12:]
                elif t == "no description":
                    cur["description"] = ""
                elif t.startswith("no e164 "):
                    cur["entries"] = [e for e in cur["entries"] if e != t[8:]]
                elif t.startswith("e164 ") and t[5:] not in cur["entries"]:
                    cur["entries"].append(t[5:])
        return res

    def load() -> list[dict]:
        maps = parse_maps(show_maps())
        used = parse_usage(show_usage())
        return [{**m, "used": used.get(t, [])} for t, m in sorted(maps.items(), key=lambda kv: int(kv[0]))]

    def find(tag: str) -> dict:
        m = next((x for x in load() if x["tag"] == tag), None)
        if not m:
            raise E164Error(f"Pattern Map {tag} gibt es nicht.")
        return m

    def verify(res: dict, tag: str, entries: list[str] | None) -> dict:
        """Nach dem Schreiben pruefen, ob der Router die Eintraege wirklich hat."""
        if not res.get("ok"):
            return res
        now = next((x for x in load() if x["tag"] == tag), None)
        if entries is None:
            ok = now is None
        else:
            ok = now is not None and sorted(now["entries"]) == sorted(entries)
        if not ok:
            tail = "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-30:])
            res.update(ok=False, error=f"Der Router hat die Pattern Map {tag} nicht wie gewünscht übernommen."
                                       + (f" Ausgabe des Routers:\n\n{tail}" if tail else ""))
        return res

    def map_lines(tag: str, desc: str | None, add: list[str], remove: list[str], old_desc: str = "") -> list[str]:
        lines = [f"voice class e164-pattern-map {tag}"]
        if desc is not None and desc != old_desc:
            lines.append(f" description {desc}" if desc else " no description")
        lines += [f" no e164 {e}" for e in remove]
        lines += [f" e164 {e}" for e in add]
        return lines

    def reply(res: dict):
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.errorhandler(E164Error)
    def on_e164_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/e164.js")
    def e164_js():
        return send_file(BASE / "static" / "e164.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/e164")
    def e164_list():
        return jsonify(ok=True, maps=load())

    @app.post("/api/e164/maps")
    def e164_create():
        b = request.get_json(force=True) or {}
        maps = load()
        used = {int(m["tag"]) for m in maps}
        tag = str(b.get("tag") or "").strip()
        if tag:
            if not tag.isdigit() or not 1 <= int(tag) <= MAX_TAG:
                raise E164Error(f"Nummer der Pattern Map: 1–{MAX_TAG}.")
            if int(tag) in used:
                raise E164Error(f"Pattern Map {tag} gibt es schon.")
        else:
            tag = str(next(t for t in range(1, MAX_TAG + 1) if t not in used))
        entries = entries_input(b.get("entries"))
        if not entries:
            raise E164Error("Mindestens ein Muster eintragen.")
        desc = ascii_text(str(b.get("description") or ""))
        res = do_apply(map_lines(tag, desc, entries, []))
        res["tag"] = tag
        return reply(verify(res, tag, entries))

    @app.put("/api/e164/maps/<tag>")
    def e164_update(tag):
        """Beschreibung und/oder komplette Eintragsliste setzen."""
        b = request.get_json(force=True) or {}
        cur = find(tag)
        desc = ascii_text(str(b["description"])) if "description" in b else None
        if "entries" in b:
            entries = entries_input(b.get("entries"))
            if not entries:
                raise E164Error("Eine Pattern Map braucht mindestens ein Muster – sonst die Map löschen.")
        else:
            entries = cur["entries"]
        add = [e for e in entries if e not in cur["entries"]]
        remove = [e for e in cur["entries"] if e not in entries]
        lines = map_lines(tag, desc, add, remove, cur["description"])
        if len(lines) == 1:
            return jsonify(ok=True, config=[], unchanged=True)
        return reply(verify(do_apply(lines), tag, entries))

    @app.post("/api/e164/maps/<tag>/entries")
    def e164_add_entries(tag):
        cur = find(tag)
        new = [e for e in entries_input((request.get_json(force=True) or {}).get("entries")) if e not in cur["entries"]]
        if not new:
            raise E164Error("Keine neuen Muster (leer oder schon vorhanden).")
        return reply(verify(do_apply(map_lines(tag, None, new, [])), tag, cur["entries"] + new))

    @app.delete("/api/e164/maps/<tag>/entries")
    def e164_delete_entry(tag):
        cur = find(tag)
        pattern = str((request.get_json(force=True) or {}).get("pattern") or "").strip()
        if pattern not in cur["entries"]:
            raise E164Error(f"Muster {pattern} ist nicht in Pattern Map {tag}.")
        rest = [e for e in cur["entries"] if e != pattern]
        if not rest:
            raise E164Error("Das ist das letzte Muster. Eine leere Pattern Map ist nicht sinnvoll – "
                            "lieber die ganze Map löschen.")
        return reply(verify(do_apply(map_lines(tag, None, [], [pattern])), tag, rest))

    @app.delete("/api/e164/maps/<tag>")
    def e164_delete(tag):
        cur = find(tag)
        if cur["used"]:
            where = ", ".join(f"Dial-Peer {u['peer']} ({u['kind']})" for u in cur["used"])
            raise E164Error(f"Pattern Map {tag} wird noch verwendet: {where}. Erst dort entfernen.")
        return reply(verify(do_apply([f"no voice class e164-pattern-map {tag}"]), tag, None))

    # ---------------- Absender-Routing: welche eigene Rufnummer geht ueber welchen Provider ----------------

    def is_provider(p: dict) -> bool:
        if p["kind"] != "voip" or not p["target"]:
            return False
        if host_ip and host_ip in p["target"]:
            return False                        # Asterisk, Recorder
        return not re.search(r"Asterisk|cme-recorder|cme-web", p["desc"])

    def egress_state() -> dict:
        peers = [p for p in parse_peers(show_peers()) if is_provider(p)]
        maps = parse_maps(show_maps())
        policies = parse_policies(show_policies())
        dns = parse_dns(show_dns())
        dn_numbers = {d["number"] for d in dns}
        assign, default, others = {}, "", {}
        for p in peers:
            m = maps.get(p["calling_map"])
            if not m:
                continue
            for e in m["entries"]:
                if e in (DEFAULT_PATTERN, "T", ".T", "+.T"):
                    default = default or p["tag"]
                elif e in dn_numbers:
                    assign[e] = p["tag"]
                else:
                    others.setdefault(p["tag"], []).append(e)
        policy = next((t for t, ls in policies.items() if POLICY_LINES[0] in ls), "")
        allpeers = [p for p in parse_peers(show_peers()) if p["kind"] == "voip"]
        # eingehende Dial-Peers: dort muss die Regel haengen (destination provision-policy)
        inbound = [{"tag": p["tag"], "desc": p["desc"], "incoming": p["incoming"],
                    "selected": bool(policy) and p["policy"] == policy}
                   for p in allpeers if p["incoming"] or (policy and p["policy"] == policy)]
        on = any(i["selected"] for i in inbound)
        active = [p["tag"] for p in peers if p["calling_map"]] if on else []
        return {"providers": [dict(p, active=p["tag"] in active, others=others.get(p["tag"], [])) for p in peers],
                "numbers": [dict(d, provider=assign.get(d["number"], "")) for d in dns],
                "default": default, "policy": policy, "policy_lines": POLICY_LINES, "policies": policies,
                "maps": maps, "inbound": inbound, "allpeers": allpeers,
                "unmanaged": [p["tag"] for p in peers if active and p["tag"] not in active]}

    @app.get("/api/egress")
    def egress_get():
        st = egress_state()
        st.pop("maps")
        st.pop("policies")
        st.pop("allpeers")
        return jsonify(ok=True, **st)

    @app.post("/api/egress")
    def egress_save():
        b = request.get_json(force=True) or {}
        st = egress_state()
        tags = {p["tag"] for p in st["providers"]}
        numbers = {n["number"] for n in st["numbers"]}
        assign = {}
        for num, tag in (b.get("assign") or {}).items():
            tag = str(tag or "")
            if not tag:
                continue
            if num not in numbers:
                raise E164Error(f"{num} ist keine Rufnummer eines Telefons.")
            if tag not in tags:
                raise E164Error(f"Dial-Peer {tag} ist kein Provider-Dial-Peer.")
            assign[num] = tag
        default = str(b.get("default") or "")
        if default and default not in tags:
            raise E164Error(f"Dial-Peer {default} ist kein Provider-Dial-Peer.")
        voip = {p["tag"]: p for p in st["allpeers"]}
        inbound = [str(t) for t in (b.get("inbound") or []) if str(t)]
        for t in inbound:
            if t not in voip:
                raise E164Error(f"Dial-Peer {t} gibt es nicht (oder ist kein VoIP-Dial-Peer).")
        maps = st["maps"]
        want: dict[str, list[str]] = {}
        for p in st["providers"]:
            entries = sorted(n for n, t in assign.items() if t == p["tag"]) + p["others"]
            if default == p["tag"]:
                entries.append(DEFAULT_PATTERN)
            if entries:
                want[p["tag"]] = entries

        if want and not inbound:
            raise E164Error("Bitte mindestens einen eingehenden Dial-Peer wählen, über den die Anrufe hereinkommen.")
        lines, tail = [], []
        policy = st["policy"]
        if want and not policy:
            policy = str(next(t for t in range(1, MAX_TAG + 1) if str(t) not in st["policies"]))
            lines += [f"voice class dial-peer provision-policy {policy}"] + [f" {l}" for l in POLICY_LINES]
        # Regel an die eingehenden Dial-Peers haengen bzw. dort entfernen
        for t, p in voip.items():
            has = bool(p["policy"]) and p["policy"] == policy
            if want and t in inbound and not has:
                if p["policy"]:
                    tail += [f"dial-peer voice {t} voip", f" no destination provision-policy {p['policy']}"]
                tail += [f"dial-peer voice {t} voip", f" destination provision-policy {policy}"]
            elif p["policy"] and (has or t in tags) and not (want and t in inbound):
                # nicht mehr gewaehlt - oder Rest der frueheren Fassung an einem Provider-Dial-Peer
                tail += [f"dial-peer voice {t} voip", f" no destination provision-policy {p['policy']}"]
        used = {int(t) for t in maps}
        for p in st["providers"]:
            entries = want.get(p["tag"], [])
            m = p["calling_map"] if p["calling_map"] in maps else ""
            if entries:
                if not m:
                    m = str(next(t for t in range(100, MAX_TAG + 1) if t not in used))
                    used.add(int(m))
                    lines += [f"voice class e164-pattern-map {m}",
                              f" description {EGRESS_DESC.format(tag=p['tag'])}"] + [f" e164 {e}" for e in entries]
                else:
                    cur = maps[m]["entries"]
                    add = [e for e in entries if e not in cur]
                    rem = [e for e in cur if e not in entries]
                    if add or rem:     # erst hinzufuegen, dann entfernen: die Map wird nie leer
                        lines += [f"voice class e164-pattern-map {m}"] + [f" e164 {e}" for e in add] + \
                                 [f" no e164 {e}" for e in rem]
                if p["calling_map"] != m:
                    tail += [f"dial-peer voice {p['tag']} voip", f" destination calling e164-pattern-map {m}"]
            elif p["calling_map"]:
                tail += [f"dial-peer voice {p['tag']} voip",
                         f" no destination calling e164-pattern-map {p['calling_map']}"]
                mm = maps.get(p["calling_map"])
                if mm and mm["description"] == EGRESS_DESC.format(tag=p["tag"]):
                    tail.append(f"no voice class e164-pattern-map {p['calling_map']}")
        lines += tail
        if not lines:
            return jsonify(ok=True, config=[], unchanged=True)
        res = do_apply(lines)
        if res.get("ok"):
            now = egress_state()
            got = {n["number"]: n["provider"] for n in now["numbers"] if n["provider"]}
            if got != assign or now["default"] != default:
                tail_out = "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-30:])
                res.update(ok=False, error="Der Router hat das Absender-Routing nicht wie gewünscht übernommen."
                                           + (f" Ausgabe des Routers:\n\n{tail_out}" if tail_out else ""))
        return reply(res)

    # Telefon-Firmware (firmware.py, Tab Telefone)
    import firmware
    firmware.register(app, router, apply)
