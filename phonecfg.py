"""
phonecfg.py - Konfigurationsdateien der Telefone (SEP<MAC>.cnf.xml) im Browser anpassen

CME erzeugt die Dateien bei jedem "create profile" neu. Deshalb speichert die Seite keine
ganze Datei, sondern die Unterschiede zur von CME erzeugten Fassung als Regeln
(Wert setzen, Element entfernen, Attribut setzen/entfernen) und wendet sie nach jedem
"create profile" erneut an. Dafuer wird router.configure() umhuellt: Enthaelt eine
Konfiguration "create profile", werden danach die angepassten Dateien neu geschrieben.

Ablage: <PHONECFG_DIR>/phonecfg.json (Regeln) und orig/<datei> (letzte Fassung von CME).
Wird aus firmware.register() eingehaengt; Oberflaeche: static/phonecfg.js (Tab Telefone).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import jsonify, request, send_file

import media

BASE = Path(__file__).resolve().parent
RE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,80}\.xml$")
NS_MARK = "__ns__"      # CME-Dateien nutzen Praefixe wie xsi:type ohne Namespace-Deklaration


class CfgError(ValueError):
    pass


# ---------------- XML ----------------

def _mask(text: str) -> str:
    # Praefixe in Attribut- und Elementnamen maskieren, damit der Parser nicht ueber
    # nicht deklarierte Namespaces stolpert
    text = re.sub(r"(\s)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)(\s*=)", rf"\1\2{NS_MARK}\3\4", text)
    return re.sub(r"(</?)([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)", rf"\1\2{NS_MARK}\3", text)


def _unmask(text: str) -> str:
    return text.replace(NS_MARK, ":")


def parse(text: str) -> ET.Element:
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip())
    try:
        return ET.fromstring(_mask(text))
    except ET.ParseError as e:
        raise CfgError(f"Kein gültiges XML: {e}")


def serialize(root: ET.Element, decl: bool) -> str:
    body = _unmask(ET.tostring(root, encoding="unicode"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' if decl else "") + body + "\n"


def index(root: ET.Element) -> dict[str, ET.Element]:
    """Pfad -> Element, Pfad wie device/sipProfile[1]/sipLines[1]/line[2] (Zaehlung je Tag)."""
    out = {}

    def walk(el, path):
        out[path] = el
        counts: dict[str, int] = {}
        for ch in el:
            counts[ch.tag] = counts.get(ch.tag, 0) + 1
            walk(ch, f"{path}/{ch.tag}[{counts[ch.tag]}]")
    walk(root, root.tag)
    return out


def leaf(el: ET.Element) -> bool:
    return len(el) == 0


def diff(orig: ET.Element, new: ET.Element) -> list[dict]:
    """Regeln, die orig in new ueberfuehren (Blattwerte, entfernte Elemente, Attribute)."""
    if orig.tag != new.tag:
        raise CfgError("Das Wurzelelement wurde geändert.")
    a, b = index(orig), index(new)
    rules = []
    for p, el in b.items():
        o = a.get(p)
        if leaf(el) and (o is None or not leaf(o) or (o.text or "").strip() != (el.text or "").strip()):
            rules.append({"op": "set", "path": p, "text": (el.text or "").strip()})
        oattr = o.attrib if o is not None else {}
        for k, v in el.attrib.items():
            if oattr.get(k) != v:
                rules.append({"op": "attr", "path": p, "name": _unmask(k), "value": v})
        for k in oattr:
            if k not in el.attrib:
                rules.append({"op": "attr", "path": p, "name": _unmask(k), "value": None})
    removed = [p for p in a if p not in b]
    # nur die obersten entfernten Elemente, tiefste zuerst entfernen
    removed = [p for p in removed if not any(p.startswith(q + "/") for q in removed if q != p)]
    for p in sorted(removed, key=lambda x: -x.count("/")):
        rules.append({"op": "del", "path": p})
    return rules


def _steps(path: str) -> list[tuple[str, int]]:
    out = []
    for part in path.split("/")[1:]:
        m = re.match(r"^(.+)\[(\d+)\]$", part)
        out.append((m.group(1), int(m.group(2))) if m else (part, 1))
    return out


def _find(root: ET.Element, path: str, create: bool) -> ET.Element | None:
    el = root
    for tag, n in _steps(path):
        kids = [c for c in el if c.tag == tag]
        if len(kids) >= n:
            el = kids[n - 1]
        elif create:
            new = None
            if len(el) and not (el[-1].tail or "").strip():
                el[-1].tail = "\n"
            for _ in range(n - len(kids)):
                new = ET.SubElement(el, tag)
                new.tail = "\n"
            el = new
        else:
            return None
    return el


def apply_rules(root: ET.Element, rules: list[dict]) -> int:
    """Regeln anwenden; gibt die Zahl der Regeln zurueck, die nicht passten."""
    misses = 0
    for r in rules:
        if r["op"] == "set":
            el = _find(root, r["path"], create=True)
            el.text = r["text"]
        elif r["op"] == "attr":
            el = _find(root, r["path"], create=True)
            key = r["name"].replace(":", NS_MARK)
            if r["value"] is None:
                el.attrib.pop(key, None)
            else:
                el.set(key, r["value"])
        elif r["op"] == "del":
            parent_path, _, _ = r["path"].rpartition("/")
            parent = _find(root, parent_path, create=False) if "/" in parent_path else root
            el = _find(root, r["path"], create=False)
            if parent is not None and el is not None:
                parent.remove(el)
            else:
                misses += 1
    return misses


def patch(text: str, rules: list[dict]) -> tuple[str, int]:
    root = parse(text)
    misses = apply_rules(root, rules)
    return serialize(root, text.lstrip().startswith("<?xml")), misses


def canon(text: str) -> str:
    """Vergleichbare Form (Leerraum zwischen Elementen egal)."""
    root = parse(text)
    for el in root.iter():
        el.text = (el.text or "").strip() or None
        el.tail = None
    return ET.tostring(root, encoding="unicode")


def describe(r: dict) -> str:
    short = re.sub(r"\[1\]", "", r["path"]).split("/", 1)[-1]
    if r["op"] == "set":
        return f"{short} = {r['text']!r}"
    if r["op"] == "del":
        return f"{short} entfernen"
    return f"{short} @{r['name']} " + ("entfernen" if r["value"] is None else f"= {r['value']!r}")


def parse_binds(out: str) -> dict[str, str]:
    """show voice register tftp-bind -> {alias: pfad} der XML-Dateien"""
    res = {}
    for line in out.splitlines():
        m = re.search(r"(\S+\.xml)\s+alias\s+(\S+\.xml)", line)
        if m:
            res[m.group(2)] = m.group(1)
    return res


# ---------------- Web ----------------

def register(app, router, apply) -> None:
    demo = type(router).__name__ == "DemoRouter"
    files = media.DemoFiles(router) if demo else media.RouterFiles(router)
    store = Path(os.environ.get("PHONECFG_DIR") or os.environ.get("VM_CONFIG_DIR") or "/data/asterisk")
    if demo:
        store = Path(tempfile.gettempdir()) / "cme-web-demo-phonecfg"
    lock = threading.Lock()

    def load_rules() -> dict:
        try:
            return json.loads((store / "phonecfg.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def save_rules(d: dict) -> None:
        store.mkdir(parents=True, exist_ok=True)
        tmp = store / ".phonecfg.json.tmp"
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
        tmp.replace(store / "phonecfg.json")

    def orig_path(name: str) -> Path:
        return store / "orig" / name

    def binds() -> dict[str, str]:
        if demo:
            return {Path(p).name: p for p in router.files if p.endswith(".cnf.xml")}
        return parse_binds(router.show("show voice register tftp-bind"))

    def pools() -> dict[str, dict]:
        out = router.show("show running-config | section voice register pool")
        res, cur = {}, None
        for line in out.splitlines():
            m = re.match(r"^voice register pool\s+(\d+)", line)
            if m:
                cur = {"pool": m.group(1), "type": "", "desc": "", "mac": ""}
                continue
            if cur is None or not line.startswith(" "):
                cur = None
                continue
            t = line.strip()
            mm = re.match(r"id mac (\S+)", t)
            if mm:
                cur["mac"] = mm.group(1).replace(".", "").upper()
                res[cur["mac"]] = cur
            elif t.startswith("type "):
                cur["type"] = t[5:]
            elif t.startswith("description "):
                cur["desc"] = t[12:]
        return res

    def write_file(path: str, text: str) -> None:
        files.delete(path)
        files.upload(text.encode("utf-8"), path)

    def reapply(only: str | None = None) -> list[str]:
        """Nach "create profile": frische Fassung von CME sichern, Regeln anwenden, zurueckschreiben."""
        rules = load_rules()
        if not rules:
            return []
        done = []
        b = binds()
        with lock:
            for name, rs in rules.items():
                if only and name != only:
                    continue
                path = b.get(name)
                if not path or not rs:
                    continue
                cur = files.read_text(path)
                if cur is None:
                    continue
                o = orig_path(name)
                try:
                    if o.exists() and canon(cur) == canon(patch(o.read_text(), rs)[0]):
                        continue    # schon angepasst, CME hat die Datei nicht neu erzeugt
                    canon(cur)
                except CfgError:
                    continue
                orig_path(name).parent.mkdir(parents=True, exist_ok=True)
                orig_path(name).write_text(cur)
                text, _ = patch(cur, rs)
                write_file(path, text)
                done.append(name)
        return done

    # router.configure umhuellen: nach "create profile" die Anpassungen erneut schreiben
    if not getattr(router, "_phonecfg_wrapped", False):
        orig_configure = router.configure

        def configure(lines):
            out = orig_configure(lines)
            if any(str(l).strip() == "create profile" for l in lines):
                try:
                    if not demo:
                        time.sleep(2)       # CME schreibt die Dateien kurz nach dem Befehl
                    reapply()
                except Exception as e:      # Anpassen darf das Speichern nicht scheitern lassen
                    out += f"\n(cme-web: Telefon-Anpassungen nicht neu geschrieben: {type(e).__name__}: {e})"
            return out
        router.configure = configure
        router._phonecfg_wrapped = True

    if demo:
        # Beispieldatei je Telefon mit MAC
        for mac, p in pools().items():
            path = f"{media.DEFAULT_BASE}/SEP{mac}.cnf.xml"
            if path not in router.files:
                router.files[path] = (
                    f'<device  xsi:type="axl:XIPPhone" ctiid="1">\n<deviceProtocol>SIP</deviceProtocol>\n'
                    f'<sshUserId>cisco</sshUserId>\n<sshPassword>cisco</sshPassword>\n'
                    f'<devicePool>\n<dateTimeSetting>\n<dateTemplate>D.M.Y</dateTemplate>\n'
                    f'<timeZone>W. Europe Standard/Daylight Time</timeZone>\n</dateTimeSetting>\n</devicePool>\n'
                    f'<sipProfile>\n<sipLines>\n<line button="1">\n<featureID>9</featureID>\n'
                    f'<featureLabel>{p["desc"] or "Leitung"}</featureLabel>\n</line>\n</sipLines>\n'
                    f'<preferredCodec>g711alaw</preferredCodec>\n</sipProfile>\n'
                    f'<vendorConfig>\n<webAccess>1</webAccess>\n<sshAccess>1</sshAccess>\n</vendorConfig>\n'
                    f'<loadInformation>sip88xx.14-4-1-0301-6</loadInformation>\n</device>\n').encode()

    def check_name(name: str) -> str:
        if not RE_NAME.match(name):
            raise CfgError("Ungültiger Dateiname.")
        return name

    # ---------------- API ----------------

    @app.errorhandler(CfgError)
    def on_cfg_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/phonecfg.js")
    def phonecfg_js():
        return send_file(BASE / "static" / "phonecfg.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/phonecfg")
    def pc_list():
        b = binds()
        ps = pools()
        rules = load_rules()
        out = []
        for name, path in sorted(b.items()):
            mac = name[3:-8].upper() if name.upper().startswith("SEP") and name.endswith(".cnf.xml") else ""
            p = ps.get(mac, {})
            out.append({"name": name, "path": path, "pool": p.get("pool", ""), "type": p.get("type", ""),
                        "desc": p.get("desc", ""), "rules": [describe(r) for r in rules.get(name, [])]})
        return jsonify(ok=True, files=out)

    @app.get("/api/phonecfg/<name>")
    def pc_get(name):
        name = check_name(name)
        path = binds().get(name)
        if not path:
            raise CfgError("Datei nicht gefunden.")
        cur = files.read_text(path)
        if cur is None:
            raise CfgError(f"{path} lässt sich nicht lesen.")
        o = orig_path(name)
        rules = load_rules().get(name, [])
        return jsonify(ok=True, name=name, path=path, text=cur, rules=[describe(r) for r in rules],
                       original=o.read_text() if o.exists() and rules else cur)

    @app.put("/api/phonecfg/<name>")
    def pc_save(name):
        name = check_name(name)
        body = request.get_json(force=True) or {}
        path = binds().get(name)
        if not path:
            raise CfgError("Datei nicht gefunden.")
        text = str(body.get("text") or "")
        new = parse(text)
        rules_all = load_rules()
        with lock:
            o = orig_path(name)
            if rules_all.get(name) and o.exists():
                base = o.read_text()
            else:
                base = files.read_text(path) or ""
                o.parent.mkdir(parents=True, exist_ok=True)
                o.write_text(base)
            rules = diff(parse(base), new)
            out, misses = patch(base, rules)
            parse(out)
            write_file(path, out)
        if rules:
            rules_all[name] = rules
        else:
            rules_all.pop(name, None)
        save_rules(rules_all)
        res = {"ok": True, "rules": [describe(r) for r in rules], "misses": misses}
        pool = str(body.get("restart_pool") or "")
        if pool.isdigit():
            r = apply([f"voice register pool {pool}", " restart"])
            res.update(ok=r.get("ok", False), error=r.get("error"))
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/phonecfg/<name>")
    def pc_reset(name):
        """Alle Anpassungen entfernen und die Fassung von CME zurueckschreiben."""
        name = check_name(name)
        path = binds().get(name)
        rules_all = load_rules()
        o = orig_path(name)
        if path and o.exists():
            write_file(path, o.read_text())
        rules_all.pop(name, None)
        save_rules(rules_all)
        o.unlink(missing_ok=True)
        return jsonify(ok=True)
