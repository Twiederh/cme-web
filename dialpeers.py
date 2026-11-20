"""
dialpeers.py - Dial-Peers, SIP-Tenants und SIP-Profile bearbeiten (Tab "Dial-Peers & Trunks")

Beide werden als Konfigurationstext bearbeitet (so wie in der running-config, ohne Kopfzeile). cme-web vergleicht
mit dem Stand am Router und schickt nur die Unterschiede:
  weggefallene Zeilen  -> "no <zeile>"  (bzw. bei "no xyz" -> "xyz")
  neue Zeilen          -> "<zeile>"
Danach wird neu gelesen und angezeigt, was der Router anders uebernommen hat (z. B. verschluesselte Passwoerter).

  dial-peer voice <tag> voip|pots
  voice class tenant <tag>
  voice class sip-profiles <tag>   (neue Regeln kommen ans Ende; die Reihenfolge bestehender bleibt)
  voice class uri <name> sip | voice class server-group <tag> | voice class sip-options-keepalive <tag>
  voice service voip | voice register global | telephony-service | sip-ua   (globale Abschnitte)
Verschachtelte Zeilen (Untermodi) werden mit ihrem Pfad geschickt (tree_diff).
"""

from __future__ import annotations

import re
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent

KINDS = {"dialpeer", "tenant", "profile", "uri", "servergroup", "keepalive", "global"}
HEADS = {"dialpeer": r"^dial-peer voice (?P<tag>\d+) (?P<type>\w+)", "tenant": r"^voice class tenant (?P<tag>\d+)",
         "profile": r"^voice class sip-profiles (?P<tag>\d+)", "uri": r"^voice class uri (?P<tag>\S+) (?P<type>sip|tel)\s*$",
         "servergroup": r"^voice class server-group (?P<tag>\d+)", "keepalive": r"^voice class sip-options-keepalive (?P<tag>\d+)"}
SECTION = {"dialpeer": "dial-peer voice", "tenant": "voice class tenant", "profile": "voice class sip-profiles",
           "uri": "voice class uri", "servergroup": "voice class server-group", "keepalive": "voice class sip-options-keepalive"}
# einmalige Abschnitte (Tag = Kurzname)
GLOBALS = {"voip": "voice service voip", "regglobal": "voice register global", "telephony": "telephony-service",
           "sipua": "sip-ua"}
RE_TAG = {"uri": r"[A-Za-z0-9_.\-]{1,32}", "global": "|".join(GLOBALS)}
RE_FORBIDDEN = re.compile(r"^(end|exit|do|!|configure|interface|router|line|username|enable|aaa|crypto(?! signaling)|no dial-peer|"
                          r"no voice class)\b", re.I)
CME_DESC = re.compile(r"\(cme-web\)|Asterisk|cme-recorder")


class DPError(ValueError):
    pass


def blocks(cfg: str, head_re: str) -> dict[str, dict]:
    """Bloecke einer Art aus der running-config -> {tag: {head, lines (mit Einrueckung, ohne 1. Leerzeichen)}}"""
    out, cur = {}, None
    for line in cfg.splitlines():
        m = re.match(head_re, line)
        if m:
            cur = out.setdefault(m.group(1), {"tag": m.group(1), "head": line.rstrip(), "lines": [],
                                              **{k: v for k, v in m.groupdict().items() if k != "tag"}})
            continue
        if not line.startswith(" ") or not line.strip():
            cur = None
            continue
        if cur is not None:
            cur["lines"].append(line.rstrip()[1:])
    return out


def norm(line: str) -> str:
    return " ".join(line.split())


# Befehle, die einen alten Wert einfach ueberschreiben: hier kein "no ..." vorweg schicken
# (z. B. wuerde "no registrar 1" auch die Zugangsdaten entfernen)
OVERRIDE = re.compile(r"^(registrar \d+|description|destination-pattern|session target|session protocol|"
                      r"session server-group|incoming called-number|answer-address|dtmf-relay|codec|voice-class codec|"
                      r"voice-class sip tenant|retry \w+|timers \w+|outbound-proxy|translation-profile incoming|"
                      r"translation-profile outgoing|preference|max-conn|destination provision-policy|bind \w+|timezone|time-format|"
                     r"date-format|max-dn|max-pool|max-ephones|max-conferences|transfer-system|source-address|voicemail|"
                     r"tftp-path|registrar server|authenticate realm|network-locale|user-locale|auto-assign|password)\b")


def override_key(line: str) -> str | None:
    m = OVERRIDE.match(line)
    return m.group(1) if m else None


def diff_lines(old: list[str], new: list[str], ordered: bool = False) -> list[str]:
    """Befehle, um aus old new zu machen (Zeilen mit relativer Einrueckung).
    ordered=True (SIP-Profile): jede Regel ist eigenstaendig, nie ueberschreiben."""
    on = [norm(l) for l in old if l.strip()]
    nn = [norm(l) for l in new if l.strip()]
    added_keys = {override_key(l) for l in nn if l not in on} - {None}
    cmds = []
    for l in on:
        if l not in nn:
            if not ordered and override_key(l) in added_keys:
                continue
            cmds.append(" " + (l[3:] if l.startswith("no ") else "no " + l))
    for raw, l in zip([x for x in new if x.strip()], nn):
        if l not in on:
            cmds.append(" " + raw.rstrip())
    return cmds


def tree(lines: list[str]) -> list[tuple[tuple[str, ...], str, str]]:
    """Zeilen mit Einrueckung -> [(Pfad der uebergeordneten Zeilen (normalisiert), Zeile normalisiert, Zeile roh)]"""
    out, stack = [], []          # stack: [(einrueckung, norm, roh)]
    for raw in lines:
        if not raw.strip():
            continue
        ind = len(raw) - len(raw.lstrip())
        while stack and stack[-1][0] >= ind:
            stack.pop()
        out.append((tuple(x[1] for x in stack), norm(raw), raw))
        stack.append((ind, norm(raw), raw))
    return out


def tree_diff(head: str, old: list[str], new: list[str]) -> list[str]:
    """Wie diff_lines, aber fuer verschachtelte Abschnitte (voice service voip / sip / ...): jede Aenderung wird
    mit ihrem Pfad geschickt, damit sie im richtigen Untermodus landet."""
    to, tn = tree(old), tree(new)
    so, sn = {(p, l) for p, l, _ in to}, {(p, l) for p, l, _ in tn}
    raw_of = {(p, l): r for p, l, r in to + tn}
    added_keys = {(p, override_key(l)) for p, l in sn - so} - {(p, None) for p, _ in sn}
    cmds, last = [], None

    def path_cmds(path):
        nonlocal last
        if path == last:
            return []
        last = path
        res = [head]
        for i in range(len(path)):
            res.append(" " * (i + 1) + raw_of.get((path[:i], path[i]), path[i]).strip())
        return res

    for p, l, _ in to:
        if (p, l) in sn:
            continue
        # Elternzeile entfernt -> Unterzeilen verschwinden mit ihr
        if any((p[:i], p[i]) not in sn for i in range(len(p))):
            continue
        if override_key(l) and (p, override_key(l)) in added_keys:
            continue
        cmds += path_cmds(p) + [" " * (len(p) + 1) + (l[3:] if l.startswith("no ") else "no " + l)]
    for p, l, r in tn:
        if (p, l) in so:
            continue
        cmds += path_cmds(p) + [" " * (len(p) + 1) + r.strip()]
        last = p + (l,)          # Unterzeilen der neuen Zeile direkt dahinter
    return cmds


def register(app, router, apply, demo: bool) -> None:  # noqa: ARG001
    def dialpeers() -> dict[str, dict]:
        res = blocks(router.show("show running-config | section dial-peer voice"), HEADS["dialpeer"])
        for p in res.values():
            info = {"desc": "", "pattern": "", "incoming": "", "target": "", "tenant": "", "codec": "",
                    "translation": "", "shutdown": False}
            for l in p["lines"]:
                t = l.strip()
                if t.startswith("description "):
                    info["desc"] = t[12:]
                elif t.startswith("destination-pattern "):
                    info["pattern"] = t.split()[1]
                elif t.startswith("incoming called"):
                    info["incoming"] = t.split()[-1]
                elif t.startswith(("session target ", "session server-group ")):
                    info["target"] = " ".join(t.split()[2:]) if t.startswith("session target") else "server-group " + t.split()[2]
                elif t.startswith("voice-class sip tenant "):
                    info["tenant"] = t.split()[3]
                elif t.startswith(("codec ", "voice-class codec ")):
                    info["codec"] = t
                elif t.startswith("translation-profile "):
                    info["translation"] = (info["translation"] + " " + " ".join(t.split()[1:])).strip()
                elif t == "shutdown":
                    info["shutdown"] = True
            p.update(info, managed=bool(CME_DESC.search(info["desc"])))
        return res

    def tenants() -> dict[str, dict]:
        res = blocks(router.show("show running-config | section voice class tenant"), HEADS["tenant"])
        for t in res.values():
            reg = next((l.strip() for l in t["lines"] if l.strip().startswith("registrar ")), "")
            users = []
            for l in t["lines"]:
                m = re.match(r"^\s*(credentials|authentication) username (\S+)", l)
                if m and m.group(2) not in users:
                    users.append(m.group(2))
            t.update(registrar=reg, users=users,
                     proxy=next((l.strip()[15:] for l in t["lines"] if l.strip().startswith("outbound-proxy ")), ""))
        return res

    def profiles() -> dict[str, dict]:
        res = blocks(router.show("show running-config | section voice class sip-profiles"), HEADS["profile"])
        for p in res.values():
            p["rules"] = len(p["lines"])
        return res

    def simple(kind: str) -> dict[str, dict]:
        res = blocks(router.show(f"show running-config | section {SECTION[kind]}"), HEADS[kind])
        return res

    def globals_() -> dict[str, dict]:
        res = {}
        for key, head in GLOBALS.items():
            out = router.show(f"show running-config | section {head}")
            lines, take = [], False
            for line in out.splitlines():
                if not line.startswith(" "):
                    take = line.strip() == head
                    continue
                if take and line.strip() and not line.strip().startswith("!"):
                    lines.append(line.rstrip()[1:])
            res[key] = {"tag": key, "head": head, "lines": lines}
        return res

    def other_uses(kind: str, tag: str) -> list[str]:
        """Wer benutzt eine URI-Klasse / Server-Gruppe / Keepalive-Profil?"""
        dps = dialpeers()
        pat = {"uri": rf"\buri(?: \w+)? {re.escape(tag)}$|^incoming uri \w+ {re.escape(tag)}$|^destination uri {re.escape(tag)}$",
               "servergroup": rf"^session server-group {re.escape(tag)}$",
               "keepalive": rf"^voice-class sip options-keepalive profile {re.escape(tag)}$"}[kind]
        return [f"Dial-Peer {p['tag']}" for p in dps.values() if any(re.search(pat, l.strip()) for l in p["lines"])]

    def uses(dps: dict) -> dict[str, list[str]]:
        u: dict[str, list[str]] = {}
        for p in dps.values():
            if p["tenant"]:
                u.setdefault(p["tenant"], []).append(p["tag"])
        return u

    def profile_uses(dps: dict, ts: dict) -> dict[str, list[str]]:
        u: dict[str, list[str]] = {}
        for p in dps.values():
            for l in p["lines"]:
                m = re.match(r"^\s*voice-class sip profiles (\d+)(.*)$", l)
                if m:
                    u.setdefault(m.group(1), []).append(f"Dial-Peer {p['tag']}{' (eingehend)' if 'inbound' in m.group(2) else ''}")
        for t in ts.values():
            for l in t["lines"]:
                m = re.match(r"^\s*sip-profiles (\d+)", l)
                if m:
                    u.setdefault(m.group(1), []).append(f"Tenant {t['tag']}")
        return u

    def parse_text(text: str) -> list[str]:
        lines = []
        for raw in str(text or "").replace("\r", "").split("\n"):
            if not raw.strip():
                continue
            if raw.strip().startswith("!"):
                continue
            if RE_FORBIDDEN.match(raw.strip()):
                raise DPError(f"Diese Zeile ist hier nicht erlaubt: {raw.strip()}")
            if len(raw) > 250:
                raise DPError("Zeile zu lang.")
            # einheitliche Einrueckung: oberste Ebene ohne Leerzeichen, Unterzeilen behalten ihre Einrueckung
            lines.append(raw.rstrip())
        base = min((len(l) - len(l.lstrip()) for l in lines), default=0)
        return [l[base:] for l in lines]

    def head_of(kind: str, tag: str, ptype: str) -> str:
        if kind == "dialpeer":
            return f"dial-peer voice {tag} {ptype}"
        if kind == "uri":
            return f"voice class uri {tag} {ptype if ptype in ('sip', 'tel') else 'sip'}"
        if kind == "global":
            return GLOBALS[tag]
        return f"{SECTION[kind]} {tag}"

    def current(kind: str) -> dict:
        if kind in ("uri", "servergroup", "keepalive"):
            return simple(kind)
        return {"dialpeer": dialpeers, "tenant": tenants, "profile": profiles, "global": globals_}[kind]()

    @app.errorhandler(DPError)
    def on_dp_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/dialpeers.js")
    def dialpeers_js():
        return send_file(BASE / "static" / "dialpeers.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/dialpeers")
    def dp_list():
        dps, ts, ps = dialpeers(), tenants(), profiles()
        u, pu = uses(dps), profile_uses(dps, ts)
        sort = lambda d: [d[k] for k in sorted(d, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))]
        ext = {}
        for kind in ("uri", "servergroup", "keepalive"):
            items = simple(kind)
            for it in items.values():
                it["used"] = other_uses(kind, it["tag"])
            ext[kind] = sort(items)
        return jsonify(ok=True,
                       dialpeers=[dps[k] for k in sorted(dps, key=int)],
                       tenants=[{**ts[k], "used": u.get(k, [])} for k in sorted(ts, key=int)],
                       profiles=[{**ps[k], "used": pu.get(k, [])} for k in sorted(ps, key=int)],
                       uris=ext["uri"], servergroups=ext["servergroup"], keepalives=ext["keepalive"],
                       globals=list(globals_().values()))

    @app.put("/api/dialpeers/<kind>/<tag>")
    def dp_save(kind, tag):
        """Body: {text, type (nur neue Dial-Peers: voip|pots), new: bool}"""
        if kind not in KINDS or not re.fullmatch(RE_TAG.get(kind, r"\d{1,10}"), tag):
            raise DPError("Ungültige Angabe.")
        b = request.get_json(force=True) or {}
        cur = current(kind)
        new_lines = parse_text(b.get("text"))
        if kind == "global":
            # "create profile" ist ein Befehl, keine Einstellung
            new_lines = [l for l in new_lines if not l.strip().startswith("create profile")]
            old_lines = [l for l in cur.get(tag, {}).get("lines", []) if not l.strip().startswith("create profile")]
            cmds = tree_diff(GLOBALS[tag], old_lines, new_lines)
            if not cmds:
                return jsonify(ok=True, config=[], unchanged=True)
            if tag == "regglobal":
                cmds += ["voice register global", " create profile"]
            res = apply(cmds)
            res["config"] = cmds
            after = [l for l in current(kind)[tag]["lines"] if not l.strip().startswith("create profile")]
            rest = [c for c in tree_diff(GLOBALS[tag], after, new_lines) if " password " not in c and c != GLOBALS[tag]]
            if rest:
                res["differences"] = rest
            res.pop("output", None)
            return jsonify(res), (200 if res.get("ok") else 400)
        if b.get("new"):
            if tag in cur:
                raise DPError(f"{ {'dialpeer': 'Dial-Peer', 'tenant': 'Tenant', 'profile': 'SIP-Profil', 'uri': 'URI-Klasse', 'servergroup': 'Server-Gruppe', 'keepalive': 'Keepalive-Profil'}[kind]} {tag} gibt es schon.")
            ptype = str(b.get("type") or "voip")
            if kind == "dialpeer" and ptype not in ("voip", "pots"):
                raise DPError("Typ: voip oder pots.")
            if kind == "uri" and ptype not in ("sip", "tel"):
                ptype = "sip"
            if not new_lines:
                raise DPError("Bitte mindestens eine Zeile eintragen.")
            old_lines, head = [], head_of(kind, tag, ptype)
        else:
            if tag not in cur:
                raise DPError("Gibt es nicht (mehr) am Router.")
            old_lines, head = cur[tag]["lines"], cur[tag]["head"]
        if kind == "profile":
            cmds = [head] + diff_lines(old_lines, new_lines, ordered=True)
            cmds = cmds if len(cmds) > 1 else []
        else:
            cmds = tree_diff(head, old_lines, new_lines)
            if b.get("new") and not cmds:
                cmds = [head]
        if not cmds:
            return jsonify(ok=True, config=[], unchanged=True)
        res = apply(cmds)
        res["config"] = cmds
        after = current(kind).get(tag)
        if res.get("ok") and after is None:
            res.update(ok=False, error="Der Router hat den Eintrag nicht angelegt.")
        if after is not None:
            rest = (diff_lines(after["lines"], new_lines, ordered=True) if kind == "profile"
                    else [c for c in tree_diff(head, after["lines"], new_lines) if c != head])
            # verschluesselte Passwoerter und Standardwerte, die der Router weglaesst, sind kein Fehler
            rest = [c for c in rest if " password " not in c]
            if rest:
                res["differences"] = rest
            res["text"] = "\n".join(after["lines"])
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.delete("/api/dialpeers/<kind>/<tag>")
    def dp_delete(kind, tag):
        if kind not in KINDS or kind == "global" or not re.fullmatch(RE_TAG.get(kind, r"\d{1,10}"), tag):
            raise DPError("Ungültige Angabe.")
        cur = current(kind)
        if tag not in cur:
            raise DPError("Gibt es nicht (mehr) am Router.")
        if kind == "tenant":
            u = uses(dialpeers()).get(tag)
            if u:
                raise DPError(f"Tenant {tag} wird noch von Dial-Peer {', '.join(u)} benutzt.")
            res = apply([f"no voice class tenant {tag}"])
        elif kind == "profile":
            u = profile_uses(dialpeers(), tenants()).get(tag)
            if u:
                raise DPError(f"SIP-Profil {tag} wird noch benutzt: {', '.join(u)}.")
            res = apply([f"no voice class sip-profiles {tag}"])
        elif kind in ("uri", "servergroup", "keepalive"):
            u = other_uses(kind, tag)
            if u:
                raise DPError(f"Wird noch benutzt: {', '.join(u)}.")
            res = apply([f"no {SECTION[kind]} {tag}" + (f" {cur[tag]['type']}" if kind == "uri" else "")])
        else:
            res = apply([f"no dial-peer voice {tag} {cur[tag]['type']}"])
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)
