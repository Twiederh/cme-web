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
        mm = re.match(r"^\s+(destination|incoming called|incoming calling)\s+e164-pattern-map\s+(\d+)", line)
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


def register(app, router, apply) -> None:
    demo = type(router).__name__ == "DemoRouter"
    demo_state = {"maps": {"1": {"tag": "1", "description": "Eigene Rufnummern", "url": "",
                                 "entries": ["+4930123456..", "030123456.."]},
                           "2": {"tag": "2", "description": "Notrufe", "url": "", "entries": ["110", "112"]}},
                  "usage": "dial-peer voice 100 voip\n incoming called e164-pattern-map 1\n"
                           "dial-peer voice 200 voip\n"}

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
            return demo_state["usage"]
        return router.show("show running-config | include ^dial-peer voice|e164-pattern-map")

    def do_apply(lines: list[str]) -> dict:
        res = apply(lines)
        if demo and res.get("ok"):
            cur = None
            for line in lines:
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
