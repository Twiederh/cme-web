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
                   "calling_map": "", "policy": "", "dest_map": "", "incoming": "", "corlist": ""}
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
        elif t.startswith("corlist outgoing "):
            cur["corlist"] = t.split()[2]
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
                  "extra": {}, "policies": {}, "cor_names": [], "cor_lists": {}}
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
                if x.get("corlist"):
                    out.append(f" corlist outgoing {x['corlist']}")
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

    # ---------------- Absender-Routing: welches Telefon geht ueber welchen Provider raus ----------------
    # CME-Telefone kommen immer ueber ihren automatischen Dial-Peer (400xx) herein; dort laesst sich keine
    # provision-policy anhaengen. Deshalb wird per COR gesteuert: jeder Provider-Dial-Peer bekommt
    #   corlist outgoing CME-OUT-VIA-<X>   (Mitglied CME-VIA-<X>)
    # und ein Telefon, das ueber diesen Provider raus soll,
    #   cor incoming CME-IN-VIA-<X> default (Mitglied CME-VIA-<X>)
    # Telefone ohne COR duerfen alle Provider benutzen, dann entscheidet wie bisher die preference.

    def is_provider(p: dict) -> bool:
        if p["kind"] != "voip" or not p["target"]:
            return False
        if host_ip and host_ip in p["target"]:
            return False                        # Asterisk, Recorder
        return not re.search(r"Asterisk|cme-recorder|cme-web", p["desc"])

    def show_pools() -> str:
        if demo:
            return "\n".join(router._blocks("voice register pool"))
        return router.show("show running-config | section voice register pool")

    def show_cor() -> str:
        if demo:
            out = ["dial-peer cor custom"] + [f" name {n}" for n in demo_state["cor_names"]]
            for n, ms in demo_state["cor_lists"].items():
                out += [f"dial-peer cor list {n}"] + [f" member {m}" for m in ms]
            return "\n".join(out)
        return router.show("show running-config | section dial-peer cor")

    def parse_pools(out: str) -> dict[str, dict]:
        pools, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^voice register pool\s+(\d+)", line)
            if m:
                cur = pools.setdefault(m.group(1), {"tag": m.group(1), "dns": [], "cor": "", "desc": "", "type": ""})
                continue
            if cur is None or not line.startswith(" "):
                cur = None
                continue
            t = line.strip()
            mm = re.match(r"^number \d+ dn (\d+)", t)
            if mm:
                cur["dns"].append(mm.group(1))
            elif t.startswith("cor incoming "):
                cur["cor"] = t.split()[2]
            elif t.startswith("description "):
                cur["desc"] = t[12:]
            elif t.startswith("type "):
                cur["type"] = t[5:]
        return pools

    def parse_cor(out: str) -> tuple[list[str], dict[str, list[str]]]:
        names, lists, cur, mode = [], {}, None, ""
        for line in out.splitlines():
            if line.startswith("dial-peer cor custom"):
                mode, cur = "custom", None
                continue
            m = re.match(r"^dial-peer cor list (\S+)", line)
            if m:
                mode, cur = "list", lists.setdefault(m.group(1), [])
                continue
            if not line.startswith(" "):
                mode, cur = "", None
                continue
            t = line.strip()
            if mode == "custom" and t.startswith("name "):
                names.append(t[5:].strip())
            elif mode == "list" and t.startswith("member "):
                cur.append(t[7:].strip())
        return names, lists

    STAGE_LISTS = {"CME-IN-INTERN", "CME-IN-NATIONAL", "CME-IN-MOBIL", "CME-IN-INTL"}

    def egress_state() -> dict:
        allpeers = parse_peers(show_peers())
        providers = [p for p in allpeers if is_provider(p)]
        pools = parse_pools(show_pools())
        names, lists = parse_cor(show_cor())
        dns = {d["dn"]: d for d in parse_dns(show_dns())}
        # Name je Provider: aus der corlist am Dial-Peer, sonst ein uebriger CME-VIA-Name (eindeutig), sonst die Nummer
        name_of: dict[str, str] = {}
        for p in providers:
            if p["corlist"].startswith("CME-OUT-VIA-"):
                name_of[p["tag"]] = p["corlist"][len("CME-OUT-VIA-"):]
        known = {n[len("CME-VIA-"):] for n in names if n.startswith("CME-VIA-")} | \
                {l[len("CME-IN-VIA-"):] for l in lists if l.startswith("CME-IN-VIA-")}
        spare = sorted(known - set(name_of.values()))
        for n in list(spare):        # z. B. "DGN" passt zu "session target dns:tel.dgn.digital"
            cand = [p["tag"] for p in providers if p["tag"] not in name_of and not p["corlist"]
                    and n.lower() in (p["desc"] + " " + p["target"]).lower()]
            if len(cand) == 1:
                name_of[cand[0]] = n
                spare.remove(n)
        unnamed = [p["tag"] for p in providers if p["tag"] not in name_of and not p["corlist"]]
        if len(spare) == 1 and len(unnamed) == 1:
            name_of[unnamed[0]] = spare[0]
        for p in providers:
            name_of.setdefault(p["tag"], p["tag"])
        prov_of_name = {v: k for k, v in name_of.items()}
        stages = sorted(t for t, p in pools.items() if p["cor"] in STAGE_LISTS)
        out_pools = []
        for t in sorted(pools, key=int):
            p = pools[t]
            prov, foreign = "", ""
            if p["cor"].startswith("CME-IN-VIA-"):
                prov = prov_of_name.get(p["cor"][len("CME-IN-VIA-"):], "")
                if not prov:
                    foreign = p["cor"]
            elif p["cor"]:
                foreign = p["cor"]
            out_pools.append({"tag": t, "desc": p["desc"], "type": p["type"], "cor": p["cor"],
                              "numbers": [{"number": dns[d]["number"], "name": dns[d]["name"]} for d in p["dns"] if d in dns],
                              "provider": prov, "foreign": foreign})
        # Reste der frueheren Fassung (Pattern Maps + provision-policy)
        maps = parse_maps(show_maps())
        policies = parse_policies(show_policies())
        ours_pol = {t for t, ls in policies.items() if len(ls) == 2 and "preference 2 called" in ls
                    and any(re.fullmatch(r"preference 1 (calling called|called calling)", l) for l in ls)}
        legacy = []
        for p in allpeers:
            m = maps.get(p["calling_map"])
            if p["calling_map"] and (m is None or m["description"].startswith("Absender fuer Dial-Peer")):
                legacy += [f"dial-peer voice {p['tag']} voip", f" no destination calling e164-pattern-map {p['calling_map']}"]
            if p["policy"] in ours_pol:
                legacy += [f"dial-peer voice {p['tag']} voip", f" no destination provision-policy {p['policy']}"]
        for t, m in maps.items():
            if m["description"].startswith("Absender fuer Dial-Peer") and m["description"].endswith("(cme-web)"):
                legacy.append(f"no voice class e164-pattern-map {t}")
        legacy += [f"no voice class dial-peer provision-policy {t}" for t in sorted(ours_pol)]
        return {"providers": [{"tag": p["tag"], "desc": p["desc"], "name": name_of[p["tag"]], "corlist": p["corlist"],
                               "dest": p["dest_map"] and f"map {p['dest_map']}" or p["pattern"],
                               "foreign": bool(p["corlist"]) and not p["corlist"].startswith("CME-OUT-VIA-")}
                              for p in providers],
                "pools": out_pools, "stages": stages, "legacy": legacy, "names": names, "lists": lists}

    @app.get("/api/egress")
    def egress_get():
        st = egress_state()
        return jsonify(ok=True, providers=st["providers"], pools=st["pools"], stages=st["stages"],
                       legacy=bool(st["legacy"]))

    def demo_egress(lines: list[str]) -> None:
        peer = pool = clist = None
        for line in lines:
            t = line.strip()
            if not line.startswith(" "):
                peer = pool = clist = None
                m = re.match(r"^dial-peer voice (\d+) voip$", t)
                if m:
                    peer = demo_state["extra"].setdefault(m.group(1), {})
                if t == "dial-peer cor custom":
                    clist = "__custom__"
                m = re.match(r"^(no )?dial-peer cor list (\S+)$", t)
                if m:
                    if m.group(1):
                        demo_state["cor_lists"].pop(m.group(2), None)
                    else:
                        clist = m.group(2)
                        demo_state["cor_lists"].setdefault(clist, [])
                continue
            if peer is not None:
                if t.startswith("corlist outgoing "):
                    peer["corlist"] = t.split()[2]
                elif t.startswith("no corlist outgoing"):
                    peer["corlist"] = ""
            elif clist == "__custom__":
                if t.startswith("no name "):
                    demo_state["cor_names"] = [n for n in demo_state["cor_names"] if n != t[8:]]
                elif t.startswith("name ") and t[5:] not in demo_state["cor_names"]:
                    demo_state["cor_names"].append(t[5:])
            elif clist:
                ms = demo_state["cor_lists"][clist]
                if t.startswith("member ") and t[7:] not in ms:
                    ms.append(t[7:])
                elif t.startswith("no member "):
                    demo_state["cor_lists"][clist] = [x for x in ms if x != t[10:]]

    @app.post("/api/egress")
    def egress_save():
        """Body: {assign: {pool-tag: provider-dial-peer-tag | ""}}"""
        b = request.get_json(force=True) or {}
        st = egress_state()
        if st["stages"]:
            raise E164Error("Die COR-Sperrstufen sind an Telefon " + ", ".join(st["stages"]) + " aktiv. Sperrstufen und "
                            "Absender-Routing lassen sich derzeit nicht gleichzeitig verwenden.")
        provs = {p["tag"]: p for p in st["providers"]}
        pools = {p["tag"]: p for p in st["pools"]}
        want: dict[str, str] = {t: p["provider"] for t, p in pools.items()}
        for tag, prov in (b.get("assign") or {}).items():
            tag, prov = str(tag), str(prov or "")
            if tag not in pools:
                raise E164Error(f"Telefon (Pool) {tag} gibt es nicht.")
            if prov and prov not in provs:
                raise E164Error(f"Dial-Peer {prov} ist kein Provider-Dial-Peer.")
            if pools[tag]["foreign"] and prov != pools[tag]["provider"]:
                raise E164Error(f"Pool {tag} hat eine eigene COR-Liste ({pools[tag]['foreign']}) – bitte erst am Router entfernen.")
            want[tag] = prov
        active = any(want.values())
        # beteiligt sind die gewaehlten Provider und alle, die dieselben Ziele bedienen (sonst koennte ein
        # Telefon dorthin ausweichen); Dial-Peers mit anderen Zielen (z. B. Webex) bleiben unberuehrt
        dests = {provs[v]["dest"] for v in want.values() if v}
        group = {t for t, p in provs.items() if (p["dest"] and p["dest"] in dests) or t in want.values()} if active else set()
        if active:
            bad = [t for t in group if provs[t]["foreign"]]
            if bad:
                raise E164Error("Provider-Dial-Peer " + ", ".join(bad) + " hat schon eine andere corlist outgoing.")
        lines: list[str] = []
        names = {p["tag"]: p["name"] for p in provs.values()}
        names_g = {t: names[t] for t in group}
        if active:
            missing = [f"CME-VIA-{n}" for n in names_g.values() if f"CME-VIA-{n}" not in st["names"]]
            if missing:
                lines += ["dial-peer cor custom"] + [f" name {n}" for n in missing]
            for n in names_g.values():
                for lst in (f"CME-OUT-VIA-{n}", f"CME-IN-VIA-{n}"):
                    if st["lists"].get(lst) != [f"CME-VIA-{n}"]:
                        lines += [f"dial-peer cor list {lst}"] + \
                                 [f" no member {m}" for m in st["lists"].get(lst, []) if m != f"CME-VIA-{n}"] + \
                                 [f" member CME-VIA-{n}"]
            for t, p in provs.items():
                if t in group and p["corlist"] != f"CME-OUT-VIA-{names[t]}":
                    lines += [f"dial-peer voice {t} voip", f" corlist outgoing CME-OUT-VIA-{names[t]}"]
                elif t not in group and p["corlist"].startswith("CME-OUT-VIA-"):
                    lines += [f"dial-peer voice {t} voip", f" no corlist outgoing {p['corlist']}"]
        tail: list[str] = []
        for t, prov in want.items():
            cur = pools[t]["cor"]
            new = f"CME-IN-VIA-{names[prov]}" if prov else ""
            if cur == new or (pools[t]["foreign"] and not prov):
                continue
            tail.append(f"voice register pool {t}")
            if cur:
                tail.append(f" no cor incoming {cur} default")
            if new:
                tail.append(f" cor incoming {new} default")
        if not active:
            for t, p in provs.items():
                if p["corlist"].startswith("CME-OUT-VIA-"):
                    tail += [f"dial-peer voice {t} voip", f" no corlist outgoing {p['corlist']}"]
            via = [l for l in st["lists"] if l.startswith(("CME-OUT-VIA-", "CME-IN-VIA-"))]
            tail += [f"no dial-peer cor list {l}" for l in via]
            vnames = [n for n in st["names"] if n.startswith("CME-VIA-")]
            if vnames:
                tail += ["dial-peer cor custom"] + [f" no name {n}" for n in vnames]
        lines += tail + st["legacy"]
        if not lines:
            return jsonify(ok=True, config=[], unchanged=True)
        res = apply(lines)
        if demo and res.get("ok"):
            demo_egress(lines)
        if res.get("ok"):
            now = {p["tag"]: p["provider"] for p in egress_state()["pools"]}
            bad = [t for t, v in want.items() if now.get(t, "") != v and not pools[t]["foreign"]]
            if bad:
                tail_out = "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-30:])
                res.update(ok=False, error="Der Router hat das Absender-Routing nicht übernommen für Pool "
                           + ", ".join(bad) + "." + (f" Ausgabe des Routers:\n\n{tail_out}" if tail_out else ""))
        res["config"] = lines
        return reply(res)

    # Telefon-Firmware (firmware.py, Tab Telefone)
    import firmware
    firmware.register(app, router, apply)
