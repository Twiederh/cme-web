"""phonebook.py - Telefonbuch fuer cme_web: Namen zu Rufnummern, Verzeichnis am Telefon, Kurzwahltasten

  - Eintraege (Nummer, Name) von Hand oder automatisch aus der Anrufliste des Routers
    (show call history voice brief, jede Minute); Namen vergibt man in der Weboberflaeche.
  - Optional Rueckwaertssuche im oeffentlichen Telefonbuch fuer neue Nummern.
  - Namen werden genutzt in der Voicemail (Web und Visual Voicemail) und beim Klingeln:
    eingehendes SIP-Profil am Trunk setzt den Anzeigenamen im From-Header.
  - Verzeichnis am Telefon (XML-Dienst /ipphone/dir, Taste Verzeichnisse ueber directoryURL).
  - Kurzwahltasten je Telefon (voice register pool / speed-dial).

Ablage: <VM_CONFIG_DIR>/phonebook.json. Wird aus voicemail.register() eingehaengt;
Oberflaeche: static/phonebook.js (Tab Telefonbuch).
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

from flask import Response, jsonify, request, send_file

BASE = Path(__file__).resolve().parent
RE_NUM = re.compile(r"^\+?[0-9*#]{2,32}$")
LOOKUP_URL = "https://www.dasoertliche.de/?form_name=search_inv&ph={number}"
EXIT = os.environ.get("VVM_EXIT_URI", "App:Close:0")
PAGE = 30           # Eintraege je Seite am Telefon (max. 32)
MAX_RULES = 200     # Namen im SIP-Profil fuer die Anzeige beim Klingeln


class PBError(ValueError):
    pass


# --------------------------------------------------------------------------
# Rufnummern
# --------------------------------------------------------------------------

def key(number: str) -> str:
    """Vergleichsform: nur Ziffern, ohne fuehrende Nullen (Amtsholung, 0, 00)."""
    return re.sub(r"\D", "", number or "").lstrip("0")


def same(a: str, b: str) -> bool:
    """Gleiche Nummer? Kurze (interne) Nummern exakt, sonst darf eine Form ein Praefix haben
    (+49 / 0049 / Amtsholung): 061871234567 == +4961871234567 == 0061871234567."""
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return False
    if min(len(ka), len(kb)) < 7:
        return ka == kb
    return ka.endswith(kb) or kb.endswith(ka)


def national(number: str) -> str:
    """Fuer die Rueckwaertssuche: deutsche Nummer national mit 0, sonst ''."""
    n = number.strip()
    k = key(n)
    if n.startswith("+") or re.match(r"^0{3}", n) or (re.match(r"^00[1-9]", n) and k.startswith("49")):
        if not k.startswith("49"):
            return ""           # Ausland
        k = k[2:]
    elif k.startswith("49") and len(k) >= 12:
        k = k[2:]
    return "0" + k if len(k) >= 6 else ""


def ascii_name(name: str, limit: int = 30) -> str:
    """Name fuer den Router (SIP-Anzeigename ohne Anfuehrungszeichen): nur Token-Zeichen."""
    t = name.replace("ß", "ss").replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue") \
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9.\-_!%*+`'~ ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit].strip()


def parse_history(out: str) -> list[dict]:
    """show call history voice brief -> eingehende Leitungsabschnitte (Answer) mit Nummer, Zeit, Dauer."""
    res, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^\s*[0-9A-Fa-f]+\s*:\s*\d+\s+(\d+)ms\.\d+\s+\(([^)]*)\)\s+[^p]*pid:(\d+)\s+(Answer|Originate)\s+(\S*)", line)
        if m:
            cur = {"ms": m.group(1), "time": m.group(2).lstrip("*"), "peer": m.group(3),
                   "dir": m.group(4), "number": m.group(5), "dur": 0}
            res.append(cur)
            continue
        m = re.match(r"^\s*dur (\d+):(\d+):(\d+)", line)
        if m and cur:
            cur["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return [r for r in res if r["dir"] == "Answer"]


def parse_lookup(page: str) -> str:
    """Namen aus einer Trefferseite der Rueckwaertssuche holen (mehrere bekannte Formen)."""
    for rx in (r'"na"\s*:\s*"([^"]{2,80})"',
               r'itemprop="name"[^>]*>\s*([^<]{2,80})<',
               r'class="[^"]*\bname\b[^"]*"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,80})<',
               r"<h2[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,80})<"):
        m = re.search(rx, page)
        if m:
            name = html.unescape(m.group(1)).strip()
            name = re.sub(r"\s+", " ", name)
            if name and not re.search(r"(?i)kein|treffer|suche|ergebnis|telefonbuch", name):
                return name
    return ""


# --------------------------------------------------------------------------
# Router-Konfiguration lesen
# --------------------------------------------------------------------------

def parse_pools(cfg: str, dns: dict[str, dict]) -> list[dict]:
    pools, cur = [], None
    for line in cfg.splitlines():
        m = re.match(r"^voice register pool\s+(\d+)\s*$", line)
        if m:
            cur = {"tag": m.group(1), "desc": "", "type": "", "lines": [], "speed": []}
            pools.append(cur)
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        mm = re.match(r'^speed-dial\s+(\d+)\s+(\S+)(?:\s+label\s+"?([^"]*)"?)?', t)
        if mm:
            cur["speed"].append({"slot": int(mm.group(1)), "number": mm.group(2), "label": (mm.group(3) or "").strip()})
        elif t.startswith("description "):
            cur["desc"] = t[12:]
        elif t.startswith("type "):
            cur["type"] = t[5:]
        else:
            mm = re.match(r"^number\s+\d+\s+dn\s+(\d+)", t)
            if mm and mm.group(1) in dns:
                cur["lines"].append(dns[mm.group(1)]["number"])
    for p in pools:
        p["speed"].sort(key=lambda s: s["slot"])
    return pools


def parse_dns(cfg: str) -> dict[str, dict]:
    dns, cur = {}, None
    for line in cfg.splitlines():
        m = re.match(r"^voice register dn\s+(\d+)\s*$", line)
        if m:
            cur = dns.setdefault(m.group(1), {"tag": m.group(1), "number": "", "name": ""})
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        if t.startswith("number "):
            cur["number"] = t.split()[1]
        elif t.startswith("name "):
            cur["name"] = t[5:].strip()
    return {k: v for k, v in dns.items() if v["number"]}


def parse_peers(cfg: str) -> list[dict]:
    peers, cur = [], None
    for line in cfg.splitlines():
        m = re.match(r"^dial-peer voice (\d+) voip", line)
        if m:
            cur = {"tag": m.group(1), "desc": "", "target": "", "pattern": "", "incoming": "", "inbound": []}
            peers.append(cur)
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        t = line.strip()
        if t.startswith("description "):
            cur["desc"] = t[12:]
        elif t.startswith("session target "):
            cur["target"] = t[15:]
        elif t.startswith("destination-pattern "):
            cur["pattern"] = t[20:]
        elif t.startswith("incoming "):
            cur["incoming"] = t
        else:
            mm = re.match(r"^voice-class sip profiles (\d+) inbound", t)
            if mm:
                cur["inbound"].append(mm.group(1))
    return peers


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------

def register(app, router, apply, demo: bool, cfgdir: Path, host_ip: str) -> None:
    store = cfgdir / "phonebook.json"
    if demo:
        store = Path(tempfile.gettempdir()) / "cme-web-demo-phonebook.json"
    lock = threading.Lock()
    status = {"poll": 0, "error": "", "added": 0}

    def load() -> dict:
        try:
            d = json.loads(store.read_text())
        except (FileNotFoundError, ValueError):
            d = {}
        d.setdefault("entries", [])
        d.setdefault("seen", [])
        s = d.setdefault("settings", {})
        s.setdefault("collect", True)
        s.setdefault("peers", [])           # Trunk-Dial-Peers (eingehend vom Provider)
        s.setdefault("ring_names", False)   # Namen beim Klingeln (SIP-Profil)
        s.setdefault("profile", 95)
        s.setdefault("lookup", False)
        s.setdefault("lookup_url", LOOKUP_URL)
        return d

    def save(d: dict) -> None:
        store.parent.mkdir(parents=True, exist_ok=True)
        d["seen"] = d["seen"][-1000:]
        tmp = store.with_name("." + store.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
        tmp.replace(store)

    if demo and not store.exists():
        now = int(time.time())
        save({"entries": [
            {"id": "d1", "number": "061871234567", "name": "Max Muster", "source": "manual", "calls": 3, "answered": 2, "last": now - 3600},
            {"id": "d2", "number": "004917012345678", "name": "", "source": "router", "calls": 1, "answered": 0, "last": now - 86400},
            {"id": "d3", "number": "0069123456", "name": "Zahnarzt Dr. Schmidt", "source": "lookup", "calls": 1, "answered": 1, "last": now - 7200},
        ], "seen": [], "settings": {}})

    def find(entries: list[dict], number: str) -> dict | None:
        """Bester Treffer: exakte Vergleichsform vor Praefix-Treffer."""
        k = key(number)
        exact = next((e for e in entries if key(e["number"]) == k), None)
        return exact or next((e for e in entries if same(e["number"], number)), None)

    def name_for(number: str) -> str:
        if not number:
            return ""
        e = find([e for e in load()["entries"] if e.get("name")], number)
        if e:
            return e["name"]
        try:
            for dn in dns_cache():
                if dn["name"] and key(dn["number"]) == key(number):
                    return dn["name"]
        except Exception:
            pass
        return ""

    dcache = {"t": 0.0, "v": []}

    def dns_cache() -> list[dict]:
        if time.time() - dcache["t"] > 60:
            dcache["v"] = list(parse_dns(router.show("show running-config | section voice register dn")).values())
            dcache["t"] = time.time()
        return dcache["v"]

    def pools() -> list[dict]:
        cfg = router.show("show running-config | section voice register")
        return parse_pools(cfg, parse_dns(cfg))

    app.config["PHONEBOOK"] = {"name": name_for, "entries": lambda: load()["entries"], "pools": pools}

    # ---------------- Rueckwaertssuche ----------------

    def reverse_lookup(number: str, url_tpl: str) -> tuple[str, str]:
        nat = national(number)
        if not nat:
            return "", "keine deutsche Rufnummer"
        url = url_tpl.replace("{number}", urllib.parse.quote(nat))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (cme-web Rueckwaertssuche)",
                                                   "Accept-Language": "de"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                page = r.read(400_000).decode("utf-8", "replace")
        except Exception as e:
            return "", f"{type(e).__name__}: {e}"
        name = parse_lookup(page)
        return name, ("gefunden" if name else "kein Eintrag")

    # ---------------- Anrufliste sammeln ----------------

    def collect_once() -> int:
        if not load()["settings"]["collect"]:
            return 0
        legs = parse_history(router.show("show call history voice brief"))
        own = {key(x["number"]) for x in dns_cache()}
        new_entries = []
        with lock:
            d = load()
            s = d["settings"]
            seen = set(d["seen"])
            for leg in legs:
                k = f'{leg["ms"]}:{leg["number"]}'
                if k in seen or not leg["number"]:
                    continue
                if s["peers"] and leg["peer"] not in s["peers"]:
                    continue
                if key(leg["number"]) in own or len(key(leg["number"])) < 6:
                    continue
                seen.add(k)
                d["seen"].append(k)
                e = find(d["entries"], leg["number"])
                if not e:
                    e = {"id": uuid.uuid4().hex[:10], "number": leg["number"], "name": "", "source": "router",
                         "calls": 0, "answered": 0, "last": 0}
                    d["entries"].append(e)
                    new_entries.append(e)
                e["calls"] = e.get("calls", 0) + 1
                if leg["dur"] > 0:
                    e["answered"] = e.get("answered", 0) + 1
                e["last"] = int(time.time())
            save(d)
        if s["lookup"]:
            for e in new_entries[:5]:
                name, _ = reverse_lookup(e["number"], s["lookup_url"])
                if name:
                    set_name(e["id"], name, "lookup")
        return len(new_entries)

    def poller():
        while True:
            try:
                status["added"] += collect_once()
                status["error"] = ""
            except Exception as e:
                status["error"] = f"{type(e).__name__}: {e}"
            status["poll"] = int(time.time())
            time.sleep(60)

    if not demo:
        threading.Thread(target=poller, daemon=True, name="phonebook-poll").start()

    def set_name(eid: str, name: str, source: str | None = None) -> None:
        with lock:
            d = load()
            for e in d["entries"]:
                if e["id"] == eid and not e.get("name"):
                    e["name"] = name
                    if source:
                        e["source"] = source
            save(d)

    # ---------------- Namen beim Klingeln (SIP-Profil am Trunk) ----------------

    def profile_lines(d: dict) -> tuple[list[str], int]:
        tag = int(d["settings"]["profile"])
        named = [e for e in d["entries"] if e.get("name") and len(key(e["number"])) >= 6]
        rules = []
        for e in sorted(named, key=lambda e: -len(key(e["number"])))[:MAX_RULES]:
            nm = ascii_name(e["name"])
            if not nm:
                continue
            k = key(e["number"])
            if k.startswith("49") and len(k) >= 12:
                k = k[2:]
            # From: "irgendwas" <sip:+4961871234567@...>  ->  From: Max Muster <sip:+4961871234567@...>
            # (ohne "?" - das ist in der IOS-CLI die Hilfe-Taste)
            rules.append(f'request INVITE sip-header From modify ":[^<]*<sip:([+]*[0-9]*{k})@" ": {nm} <sip:\\1@"')
        return rules, tag

    def sync_router() -> dict:
        d = load()
        s = d["settings"]
        rules, tag = profile_lines(d)
        cur = router.show(f"show running-config | section voice class sip-profiles {tag}")
        old = sorted({int(n) for n in re.findall(r"^\s+rule (\d+)\s", cur, re.M)})
        peers = parse_peers(router.show("show running-config | section dial-peer voice"))
        want = set(s["peers"]) if s["ring_names"] and rules else set()
        # pro Dial-Peer gibt es nur EIN eingehendes Profil - ein vorhandenes anderes nicht still ersetzen
        busy = [f"Dial-Peer {p['tag']} (Profil {', '.join(p['inbound'])})" for p in peers
                if p["tag"] in want and p["inbound"] and str(tag) not in p["inbound"]]
        if busy:
            return {"ok": False, "error": "Diese Dial-Peers haben schon ein anderes eingehendes SIP-Profil "
                                          f"(voice-class sip profiles N inbound): {', '.join(busy)}. "
                                          "Ein Dial-Peer kann nur eines haben - die Namensregel würde es ersetzen. "
                                          "Bitte die Regeln dort zusammenführen oder den Dial-Peer abwählen."}
        lines = []
        if want:
            lines.append(f"voice class sip-profiles {tag}")
            lines += [f" no rule {n}" for n in old]
            lines += [f" rule {i} {r}" for i, r in enumerate(rules, 1)]
            lines += ["voice service voip", " sip", "  sip-profiles inbound"]
        for p in peers:
            has = str(tag) in p["inbound"]
            if p["tag"] in want and not has:
                lines += [f"dial-peer voice {p['tag']} voip", f" voice-class sip profiles {tag} inbound"]
            elif p["tag"] not in want and has:
                lines += [f"dial-peer voice {p['tag']} voip", f" no voice-class sip profiles {tag} inbound"]
        if not want and old:
            lines.append(f"no voice class sip-profiles {tag}")
        if not lines:
            return {"ok": True, "rules": len(rules), "config": []}
        res = apply(lines)
        res["rules"] = len(rules) if want else 0
        return res

    # ---------------- Verzeichnis am Telefon ----------------

    def phone_only() -> bool:
        return request.environ.get("cme.phone") == "1"

    def dir_entries() -> list[tuple[str, str]]:
        out = {}
        for e in load()["entries"]:
            if e.get("name"):
                out[(e["name"], e["number"])] = 1
        try:
            for dn in dns_cache():
                if dn["name"]:
                    out[(dn["name"], dn["number"])] = 1
        except Exception:
            pass
        return sorted(out, key=lambda x: x[0].lower())

    def xml(body: str) -> Response:
        r = Response('<?xml version="1.0" encoding="UTF-8"?>\n' + body, mimetype="text/xml")
        r.headers["Content-Type"] = "text/xml; charset=utf-8"
        r.headers["Cache-Control"] = "no-cache"
        return r

    @app.get("/ipphone/dir")
    def phone_dir():
        if not phone_only():
            return Response("Nicht gefunden", status=404)
        q = (request.args.get("q") or "").strip().lower()
        try:
            page = max(0, int(request.args.get("p") or 0))
        except ValueError:
            page = 0
        ents = [x for x in dir_entries() if not q or q in x[0].lower() or q in re.sub(r"\D", "", x[1])]
        total = len(ents)
        part = ents[page * PAGE:(page + 1) * PAGE]
        base = request.host_url.rstrip("/") + "/ipphone/dir"
        qs = f"q={urllib.parse.quote(q)}&" if q else ""
        prompt = (f"Suche: {q} – " if q else "") + (f"{page * PAGE + 1}–{page * PAGE + len(part)} von {total}"
                                                    if total else "Keine Einträge")
        keys = [("Wählen", "SoftKey:Dial"), ("Bearb.", "SoftKey:EditDial"), ("Suchen", f"{base}/search")]
        if (page + 1) * PAGE < total:
            keys.append(("Weiter", f"{base}?{qs}p={page + 1}"))
        if page > 0:
            keys.append(("Zurück", f"{base}?{qs}p={page - 1}"))
        keys.append(("Beenden", EXIT))
        body = "".join(f"<DirectoryEntry><Name>{escape(n[:32])}</Name><Telephone>{escape(t)}</Telephone></DirectoryEntry>"
                       for n, t in part)
        sk = "".join(f"<SoftKeyItem><Name>{escape(n)}</Name><URL>{escape(u)}</URL><Position>{i}</Position></SoftKeyItem>"
                     for i, (n, u) in enumerate(keys, 1))
        return xml(f"<CiscoIPPhoneDirectory><Title>Telefonbuch</Title><Prompt>{escape(prompt)}</Prompt>"
                   f"{body}{sk}</CiscoIPPhoneDirectory>")

    @app.get("/ipphone/dir/search")
    def phone_dir_search():
        if not phone_only():
            return Response("Nicht gefunden", status=404)
        base = request.host_url.rstrip("/") + "/ipphone/dir"
        return xml(f"<CiscoIPPhoneInput><Title>Telefonbuch</Title><Prompt>Name oder Nummer</Prompt>"
                   f"<URL>{escape(base)}</URL><InputItem><DisplayName>Suche</DisplayName>"
                   f"<QueryStringParam>q</QueryStringParam><DefaultValue></DefaultValue><InputFlags>A</InputFlags></InputItem>"
                   f"</CiscoIPPhoneInput>")

    # ---------------- API ----------------

    @app.errorhandler(PBError)
    def on_pb_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/phonebook.js")
    def phonebook_js():
        return send_file(BASE / "static" / "phonebook.js", mimetype="text/javascript", max_age=0)

    def dir_url() -> str:
        import vvm
        return f"http://{host_ip or '<docker-host>'}:{vvm.URL_PORT}/ipphone/dir"

    @app.get("/api/phonebook")
    def pb_get():
        d = load()
        try:
            peers = parse_peers(router.show("show running-config | section dial-peer voice"))
        except Exception:
            peers = []
        pc = app.config.get("PHONECFG")
        st = pc["state"](["device/directoryURL[1]"]) if pc else {}
        return jsonify(ok=True, entries=sorted(d["entries"], key=lambda e: (bool(e.get("name")), -e.get("last", 0))),
                       settings=d["settings"], status=status, demo=demo,
                       peers=[{"tag": p["tag"], "desc": p["desc"], "target": p["target"], "pattern": p["pattern"],
                               "incoming": p["incoming"]} for p in peers],
                       dir_url=dir_url(), dir_phones=sum(v.get("device/directoryURL[1]") == dir_url() for v in st.values()),
                       phones=len(st))

    @app.post("/api/phonebook/entries")
    def pb_add():
        b = request.get_json(force=True) or {}
        number = re.sub(r"[\s/()-]", "", str(b.get("number") or ""))
        name = str(b.get("name") or "").strip()[:60]
        if not RE_NUM.match(number):
            raise PBError("Ungültige Rufnummer (Ziffern, optional + am Anfang).")
        with lock:
            d = load()
            e = find(d["entries"], number)
            if e and key(e["number"]) == key(number):
                e["name"] = name or e["name"]
            else:
                d["entries"].append({"id": uuid.uuid4().hex[:10], "number": number, "name": name, "source": "manual",
                                     "calls": 0, "answered": 0, "last": 0})
            save(d)
        return jsonify(ok=True, router=maybe_sync())

    @app.put("/api/phonebook/entries/<eid>")
    def pb_put(eid):
        b = request.get_json(force=True) or {}
        with lock:
            d = load()
            e = next((x for x in d["entries"] if x["id"] == eid), None)
            if not e:
                raise PBError("Eintrag nicht gefunden.")
            if "name" in b:
                e["name"] = str(b["name"] or "").strip()[:60]
                if e.get("source") == "lookup":
                    e["source"] = "manual"
            if "number" in b:
                n = re.sub(r"[\s/()-]", "", str(b["number"] or ""))
                if not RE_NUM.match(n):
                    raise PBError("Ungültige Rufnummer.")
                e["number"] = n
            save(d)
        return jsonify(ok=True, router=maybe_sync())

    @app.delete("/api/phonebook/entries/<eid>")
    def pb_del(eid):
        with lock:
            d = load()
            d["entries"] = [x for x in d["entries"] if x["id"] != eid]
            save(d)
        return jsonify(ok=True, router=maybe_sync())

    def maybe_sync():
        s = load()["settings"]
        if not s["ring_names"] or not s["peers"]:
            return None
        try:
            r = sync_router()
            return {"ok": r.get("ok"), "error": r.get("error"), "rules": r.get("rules")}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @app.put("/api/phonebook/settings")
    def pb_settings():
        b = request.get_json(force=True) or {}
        with lock:
            d = load()
            s = d["settings"]
            if "collect" in b:
                s["collect"] = bool(b["collect"])
            if "peers" in b:
                s["peers"] = [str(p) for p in b["peers"] if str(p).isdigit()]
            if "ring_names" in b:
                s["ring_names"] = bool(b["ring_names"])
            if "profile" in b:
                try:
                    t = int(b["profile"])
                except (TypeError, ValueError):
                    raise PBError("Profil-Nummer muss eine Zahl sein.")
                if not 1 <= t <= 10000 or t == 1:
                    raise PBError("Profil-Nummer 2–10000 (1 ist das Profil für sipgate).")
                s["profile"] = t
            if "lookup" in b:
                s["lookup"] = bool(b["lookup"])
            if "lookup_url" in b:
                u = str(b["lookup_url"] or "").strip() or LOOKUP_URL
                if not u.startswith(("http://", "https://")) or "{number}" not in u:
                    raise PBError("Adresse der Rückwärtssuche: http(s)://… mit {number}.")
                s["lookup_url"] = u
            save(d)
        res = {"ok": True}
        if any(k in b for k in ("peers", "ring_names", "profile")):
            r = sync_router()
            res.update(ok=r.get("ok", False), error=r.get("error"), rules=r.get("rules"))
        return jsonify(res), (200 if res["ok"] else 400)

    @app.post("/api/phonebook/collect")
    def pb_collect():
        try:
            n = collect_once()
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 400
        status["poll"] = int(time.time())
        return jsonify(ok=True, added=n)

    @app.post("/api/phonebook/lookup")
    def pb_lookup():
        b = request.get_json(force=True) or {}
        number = str(b.get("number") or "")
        if not RE_NUM.match(re.sub(r"[\s/()-]", "", number)):
            raise PBError("Ungültige Rufnummer.")
        name, info = reverse_lookup(number, load()["settings"]["lookup_url"])
        if name and b.get("id"):
            set_name(str(b["id"]), name, "lookup")
        return jsonify(ok=True, name=name, info=info, national=national(number))

    @app.post("/api/phonebook/directory")
    def pb_directory():
        pc = app.config.get("PHONECFG")
        if not pc:
            raise PBError("Telefon-Konfiguration nicht verfügbar.")
        if not host_ip:
            raise PBError("VM_HOST_IP ist nicht gesetzt.")
        b = request.get_json(force=True) or {}
        res = pc["set_all"]({"device/directoryURL[1]": dir_url() if b.get("enabled") else None}, bool(b.get("restart")))
        return jsonify(ok=True, **res)

    # ---------------- Kurzwahltasten ----------------

    @app.get("/api/phonebook/speeddials")
    def sd_get():
        cfg = router.show("show running-config | section voice register")
        pools = parse_pools(cfg, parse_dns(cfg))
        return jsonify(ok=True, pools=pools)

    @app.put("/api/phonebook/speeddials/<pool>")
    def sd_put(pool):
        if not pool.isdigit():
            raise PBError("Ungültiger Pool.")
        b = request.get_json(force=True) or {}
        cfg = router.show("show running-config | section voice register")
        p = next((x for x in parse_pools(cfg, parse_dns(cfg)) if x["tag"] == pool), None)
        if not p:
            raise PBError(f"Pool {pool} gibt es nicht.")
        items = []
        for it in (b.get("speed") or [])[:24]:
            n = re.sub(r"[\s/()-]", "", str(it.get("number") or ""))
            if not n:
                continue
            if not RE_NUM.match(n):
                raise PBError(f"Ungültige Nummer: {n}")
            label = re.sub(r'["\\?]', "", str(it.get("label") or "")).strip()[:30]
            items.append((n, label))
        lines = [f"voice register pool {pool}"] + [f" no speed-dial {s['slot']}" for s in p["speed"]]
        for i, (n, label) in enumerate(items, 1):
            lines.append(f' speed-dial {i} {n}' + (f' label "{label}"' if label else ""))
        lines += ["voice register global", " create profile"]
        if b.get("restart"):
            lines += [f"voice register pool {pool}", " restart"]
        res = apply(lines)
        return jsonify(res), (200 if res.get("ok") else 400)
