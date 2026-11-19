"""
translation.py - Voice Translation Rules und Translation Profiles fuer cme_web

  voice translation-rule <tag>
   rule <1-15> /<muster>/ /<ersetzung>/ [type <von> <nach>] [plan <von> <nach>]
   rule <1-15> reject /<muster>/
  voice translation-profile <name>
   translate calling|called|redirect-called <tag>

Die Oberflaeche (static/translation.js) wird von cme_web in index.html eingehaengt.
"""

from __future__ import annotations

import re
from pathlib import Path

from flask import jsonify, request, send_file

BASE = Path(__file__).resolve().parent

NUMBER_TYPES = ["abbreviated", "any", "international", "national", "network", "reserved",
                "subscriber", "unknown"]
NUMBER_PLANS = ["any", "data", "ermes", "isdn", "national", "private", "reserved", "telex", "unknown"]
# Welche Nummer ein Profil uebersetzt
TRANSLATE_KINDS = ["calling", "called", "redirect-called"]

# Regex-Teil fuer /.../, inkl. \/ innerhalb
_SLASHED = r"/((?:[^/\\]|\\.)*)/"
RE_RULE = re.compile(rf"^rule (\d+) (?:(reject) )?{_SLASHED}(?: {_SLASHED})?\s*(.*)$")
# Erlaubte Zeichen in Muster und Ersetzung (IOS-Regex, keine Leerzeichen, kein unmaskiertes /)
RE_PATTERN = re.compile(r"^(?:[0-9A-Za-z^$.*+?()\[\]{}|#,_\-]|\\[^\s])*$")
RE_PROFILE = re.compile(r"^[A-Za-z0-9_\-]{1,30}$")
RE_TESTNUM = re.compile(r"^\+?[0-9*#A-Da-d]{1,32}$")


class TranslationError(ValueError):
    pass


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def parse_rule_line(s: str) -> dict | None:
    m = RE_RULE.match(s)
    if not m:
        return None
    rest = m.group(5).split()
    r = {"prec": int(m.group(1)), "reject": bool(m.group(2)), "match": m.group(3),
         "replace": m.group(4) or "", "type_from": "", "type_to": "", "plan_from": "", "plan_to": "",
         "raw": s}
    i = 0
    while i < len(rest):
        if rest[i] in ("type", "plan") and i + 2 < len(rest):
            r[f"{rest[i]}_from"], r[f"{rest[i]}_to"] = rest[i + 1], rest[i + 2]
            i += 3
        else:
            i += 1
    return r


def parse_translation(out: str) -> tuple[dict, dict]:
    """show running-config | section voice translation -> (rulesets, profiles)"""
    rules: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"^voice translation-rule\s+(\d+)\s*$", line)
        if m:
            cur = rules.setdefault(m.group(1), {"tag": m.group(1), "rules": [], "other": []})
            cur["_kind"] = "rule"
            continue
        m = re.match(r"^voice translation-profile\s+(\S+)\s*$", line)
        if m:
            cur = profiles.setdefault(m.group(1), {"name": m.group(1), "translate": {}})
            cur["_kind"] = "profile"
            continue
        if not line.startswith(" "):
            cur = None
            continue
        if cur is None:
            continue
        s = line.strip()
        if cur["_kind"] == "rule":
            r = parse_rule_line(s)
            if r:
                cur["rules"].append(r)
            else:
                cur["other"].append(s)
        else:
            mm = re.match(r"^translate (\S+) (\d+)", s)
            if mm:
                cur["translate"][mm.group(1)] = mm.group(2)
    for d in list(rules.values()) + list(profiles.values()):
        d.pop("_kind", None)
    for r in rules.values():
        r["rules"].sort(key=lambda x: x["prec"])
    return rules, profiles


def parse_profile_usage(out: str) -> dict[str, list[dict]]:
    """show running-config | include ^dial-peer voice|^voice register pool|translation-profile
    -> {profilname: [{where, id, dir}]}"""
    used: dict[str, list[dict]] = {}
    parent = None
    for line in out.splitlines():
        m = re.match(r"^dial-peer voice (\d+)", line)
        if m:
            parent = ("Dial-Peer", m.group(1))
            continue
        m = re.match(r"^voice register pool\s+(\d+)", line)
        if m:
            parent = ("Pool", m.group(1))
            continue
        m = re.match(r"^\s+translation-profile (incoming|outgoing) (\S+)", line)
        if m and parent:
            used.setdefault(m.group(2), []).append(
                {"where": parent[0], "id": parent[1], "dir": m.group(1)})
        elif not line.startswith(" "):
            parent = None
    return used


# --------------------------------------------------------------------------
# Befehle
# --------------------------------------------------------------------------

def rule_cmd(r: dict) -> str:
    if r["reject"]:
        return f" rule {r['prec']} reject /{r['match']}/"
    s = f" rule {r['prec']} /{r['match']}/ /{r['replace']}/"
    if r["type_from"]:
        s += f" type {r['type_from']} {r['type_to']}"
    if r["plan_from"]:
        s += f" plan {r['plan_from']} {r['plan_to']}"
    return s


def same_rule(a: dict, b: dict) -> bool:
    keys = ("prec", "reject", "match", "replace", "type_from", "type_to", "plan_from", "plan_to")
    return all(a[k] == b[k] for k in keys)


def rules_input(data: dict) -> list[dict]:
    rows = data.get("rules") or []
    if not isinstance(rows, list) or not rows:
        raise TranslationError("Mindestens eine Regel eintragen.")
    if len(rows) > 15:
        raise TranslationError("Hoechstens 15 Regeln je Regelsatz.")
    out = []
    for i, row in enumerate(rows, 1):
        try:
            prec = int(str(row.get("prec") or i))
        except ValueError:
            raise TranslationError(f"Regel {i}: Nummer muss eine Zahl sein.")
        if not 1 <= prec <= 15:
            raise TranslationError(f"Regel {i}: Nummer muss zwischen 1 und 15 liegen.")
        match = str(row.get("match") or "").strip()
        replace = str(row.get("replace") or "").strip()
        reject = bool(row.get("reject"))
        if not match:
            raise TranslationError(f"Regel {prec}: Muster fehlt.")
        for what, v in (("Muster", match), ("Ersetzung", replace)):
            if len(v) > 64 or not RE_PATTERN.match(v):
                raise TranslationError(f"Regel {prec}: {what} enthaelt unzulaessige Zeichen "
                                       "(keine Leerzeichen, / nur als \\/).")
        r = {"prec": prec, "reject": reject, "match": match, "replace": "" if reject else replace,
             "type_from": "", "type_to": "", "plan_from": "", "plan_to": ""}
        if not reject:
            for key, allowed in (("type", NUMBER_TYPES), ("plan", NUMBER_PLANS)):
                f, t = str(row.get(f"{key}_from") or ""), str(row.get(f"{key}_to") or "")
                if f or t:
                    if f not in allowed or t not in allowed:
                        raise TranslationError(f"Regel {prec}: ungueltiger Nummern-{key}.")
                    r[f"{key}_from"], r[f"{key}_to"] = f, t
        out.append(r)
    precs = [r["prec"] for r in out]
    if len(set(precs)) != len(precs):
        raise TranslationError("Eine Regelnummer ist doppelt vergeben.")
    return sorted(out, key=lambda r: r["prec"])


def ruleset_lines(tag: str, new: list[dict], old: list[dict] | None) -> list[str]:
    lines = [f"voice translation-rule {tag}"]
    old_by = {r["prec"]: r for r in (old or [])}
    new_by = {r["prec"]: r for r in new}
    # Entfernte und geaenderte Regeln zuerst loeschen, sonst meldet IOS "already exists"
    for prec, r in sorted(old_by.items()):
        if prec not in new_by or not same_rule(r, new_by[prec]):
            lines.append(f" no rule {prec}")
    for prec, r in sorted(new_by.items()):
        if prec not in old_by or not same_rule(old_by[prec], r):
            lines.append(rule_cmd(r))
    return lines


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

def demo_seed(router) -> None:
    router.cfg += [
        "voice translation-rule 1", " rule 1 /^0\\(.*\\)/ /+49\\1/",
        "voice translation-rule 2", " rule 1 /^\\+49\\(.*\\)/ /0\\1/", " rule 2 reject /^0900/",
        "voice translation-profile TRUNK-IN", " translate calling 2",
        "voice translation-profile TRUNK-OUT", " translate calling 1", " translate called 1",
    ]
    router.dialpeers += " translation-profile outgoing TRUNK-OUT\n"


def _ios_to_py(pattern: str) -> str:
    # In IOS-Regeln sind \( \) Gruppen; einfache Klammern gibt es dort kaum
    return pattern.replace("\\(", "(").replace("\\)", ")")


def demo_show(router, cmd: str) -> str | None:
    if cmd.startswith("show running-config | section voice translation"):
        return "\n".join(router._blocks("voice translation"))
    if cmd.startswith("show running-config | include ^dial-peer voice|"):
        out = []
        for l in router.dialpeers.splitlines() + router.cfg:
            if l.startswith("dial-peer voice") or l.startswith("voice register pool") \
                    or "translation-profile" in l:
                out.append(l)
        return "\n".join(out)
    m = re.match(r"^test voice translation-rule (\d+) (\S+)", cmd)
    if m:
        rules, _ = parse_translation("\n".join(router._blocks("voice translation")))
        rs = rules.get(m.group(1))
        if not rs:
            return f"% Translation rule {m.group(1)} doesn't exist"
        num = m.group(2)
        for r in rs["rules"]:
            if re.search(_ios_to_py(r["match"]), num):
                if r["reject"]:
                    return f"Matched with rule {r['prec']}\nThe number is rejected"
                new = re.sub(_ios_to_py(r["match"]), r["replace"], num, count=1)
                return (f"Matched with rule {r['prec']}\n"
                        f"Original number: {num}        Translated number: {new}\n"
                        f"Original number type: none    Translated number type: none")
        return f"Didn't match with any of rules"
    return None


# --------------------------------------------------------------------------
# Web
# --------------------------------------------------------------------------

def register(app, router, apply) -> None:

    def load() -> dict:
        rules, profiles = parse_translation(router.show("show running-config | section voice translation"))
        usage = parse_profile_usage(router.show(
            "show running-config | include ^dial-peer voice|^voice register pool|translation-profile"))
        used_by: dict[str, list[dict]] = {}
        for p in profiles.values():
            for kind, tag in p["translate"].items():
                used_by.setdefault(tag, []).append({"profile": p["name"], "kind": kind})
        return {
            "rulesets": [{**r, "profiles": used_by.get(t, [])}
                         for t, r in sorted(rules.items(), key=lambda kv: int(kv[0]))],
            "profiles": [{**p, "used": usage.get(n, [])} for n, p in sorted(profiles.items())],
        }

    def fail(msg: str, code: int = 400):
        return jsonify(ok=False, error=msg), code

    @app.errorhandler(TranslationError)
    def on_err(e):
        return fail(str(e))

    @app.get("/static/<any('translation.js', 'tabs.js'):name>")
    def module_js(name):
        return send_file(BASE / "static" / name, mimetype="text/javascript", max_age=0)

    @app.get("/api/translation")
    def get_all():
        return jsonify(ok=True, number_types=NUMBER_TYPES, number_plans=NUMBER_PLANS,
                       translate_kinds=TRANSLATE_KINDS, **load())

    def verify_ruleset(res: dict, tag: str, rules: list[dict]) -> None:
        if not res["ok"]:
            return
        now = next((r for r in load()["rulesets"] if r["tag"] == tag), None)
        if now is None or [x["prec"] for x in now["rules"]] != [x["prec"] for x in rules] \
                or not all(same_rule(a, b) for a, b in zip(now["rules"], rules)):
            tail = "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-30:])
            res.update(ok=False, error=f"Der Router hat den Regelsatz {tag} nicht wie gewuenscht "
                                       f"uebernommen. Ausgabe des Routers:\n\n{tail}")

    @app.post("/api/translation/rules")
    def add_ruleset():
        data = request.get_json(force=True) or {}
        rules = rules_input(data)
        st = load()
        used = {int(r["tag"]) for r in st["rulesets"]}
        tag = str(data.get("tag") or "").strip()
        if tag:
            if not tag.isdigit() or not 1 <= int(tag) <= 2147483647:
                raise TranslationError("Ungueltige Nummer fuer den Regelsatz.")
            if int(tag) in used:
                raise TranslationError(f"Regelsatz {tag} gibt es schon.")
        else:
            tag = str(next(i for i in range(1, 100000) if i not in used))
        res = apply(ruleset_lines(tag, rules, None))
        res["tag"] = tag
        verify_ruleset(res, tag, rules)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/translation/rules/<tag>")
    def edit_ruleset(tag):
        if not tag.isdigit():
            raise TranslationError("Ungueltiger Regelsatz")
        rules = rules_input(request.get_json(force=True) or {})
        cur = next((r for r in load()["rulesets"] if r["tag"] == tag), None)
        if not cur:
            raise TranslationError(f"Regelsatz {tag} gibt es nicht.")
        lines = ruleset_lines(tag, rules, cur["rules"])
        if len(lines) == 1:
            return jsonify(ok=True, error=None, config=[], tag=tag, unchanged=True)
        res = apply(lines)
        res["tag"] = tag
        verify_ruleset(res, tag, rules)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/translation/rules/<tag>")
    def delete_ruleset(tag):
        if not tag.isdigit():
            raise TranslationError("Ungueltiger Regelsatz")
        cur = next((r for r in load()["rulesets"] if r["tag"] == tag), None)
        if not cur:
            raise TranslationError(f"Regelsatz {tag} gibt es nicht.")
        if cur["profiles"]:
            raise TranslationError(f"Regelsatz {tag} wird noch verwendet in Profil " +
                                   ", ".join(sorted({p['profile'] for p in cur['profiles']})) + ".")
        res = apply([f"no voice translation-rule {tag}"])
        res["tag"] = tag
        return jsonify(res), (200 if res["ok"] else 400)

    def run_test(tag: str, number: str) -> dict:
        """test voice translation-rule am Router ausfuehren und auswerten."""
        if not RE_TESTNUM.match(number):
            raise TranslationError("Testnummer: nur Ziffern, *, # und ein + am Anfang.")
        out = router.show(f"test voice translation-rule {tag} {number}")
        if "% " in out and "Matched" not in out:
            raise TranslationError(out.strip())
        m = re.search(r"Translated number:\s*(\S+)", out)
        t = re.search(r"Translated number type:\s*(\S+)", out)
        matched = re.search(r"Matched with rule (\d+)", out)
        return {"tag": tag, "number": number, "output": out.strip(), "translated": m.group(1) if m else None,
                "type": t.group(1) if t else None, "rule": matched.group(1) if matched else None,
                "rejected": "reject" in out.lower()}

    @app.post("/api/translation/test")
    def test_rule():
        data = request.get_json(force=True) or {}
        tag = str(data.get("tag") or "")
        number = str(data.get("number") or "").strip()
        if not tag.isdigit():
            raise TranslationError("Regelsatz auswaehlen.")
        return jsonify(ok=True, **run_test(tag, number))

    @app.post("/api/translation/profiles/<name>/test")
    def test_profile(name):
        """Testet ein Profil: je Nummernart den zugeordneten Regelsatz am Router."""
        if not RE_PROFILE.match(name):
            raise TranslationError("Ungueltiger Profilname")
        p = next((x for x in load()["profiles"] if x["name"] == name), None)
        if not p:
            raise TranslationError(f"Profil {name} gibt es nicht.")
        data = request.get_json(force=True) or {}
        results = []
        for kind in TRANSLATE_KINDS:
            number = str(data.get(kind) or "").strip()
            if not number:
                continue
            tag = p["translate"].get(kind)
            results.append({"kind": kind, "number": number, **(run_test(tag, number) if tag else {"tag": None})})
        if not results:
            raise TranslationError("Mindestens eine Nummer eingeben.")
        return jsonify(ok=True, name=name, results=results)

    # ---------------- Profile ----------------

    def profile_input(data: dict, rulesets: list[dict]) -> dict:
        known = {r["tag"] for r in rulesets}
        tr = {}
        for kind in TRANSLATE_KINDS:
            v = str((data.get("translate") or {}).get(kind) or "").strip()
            if v:
                if v not in known:
                    raise TranslationError(f"Regelsatz {v} gibt es nicht.")
                tr[kind] = v
        if not tr:
            raise TranslationError("Mindestens einen Regelsatz zuordnen.")
        return tr

    def profile_lines(name: str, new: dict, old: dict | None) -> list[str]:
        lines = [f"voice translation-profile {name}"]
        old = old or {}
        for kind in TRANSLATE_KINDS:
            if kind in old and old[kind] != new.get(kind):
                lines.append(f" no translate {kind} {old[kind]}")
            if kind in new and old.get(kind) != new[kind]:
                lines.append(f" translate {kind} {new[kind]}")
        return lines

    def verify_profile(res: dict, name: str, tr: dict) -> None:
        if not res["ok"]:
            return
        now = next((p for p in load()["profiles"] if p["name"] == name), None)
        want = {k: v for k, v in tr.items()}
        have = {k: v for k, v in (now or {}).get("translate", {}).items() if k in TRANSLATE_KINDS}
        if now is None or have != want:
            tail = "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-30:])
            res.update(ok=False, error=f"Der Router hat das Profil {name} nicht wie gewuenscht "
                                       f"uebernommen. Ausgabe des Routers:\n\n{tail}")

    @app.post("/api/translation/profiles")
    def add_profile():
        data = request.get_json(force=True) or {}
        name = str(data.get("name") or "").strip()
        if not RE_PROFILE.match(name):
            raise TranslationError("Profilname: 1-30 Zeichen, Buchstaben, Ziffern, - und _.")
        st = load()
        if any(p["name"].lower() == name.lower() for p in st["profiles"]):
            raise TranslationError(f"Profil {name} gibt es schon.")
        tr = profile_input(data, st["rulesets"])
        res = apply(profile_lines(name, tr, None))
        res["name"] = name
        verify_profile(res, name, tr)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.put("/api/translation/profiles/<name>")
    def edit_profile(name):
        st = load()
        cur = next((p for p in st["profiles"] if p["name"] == name), None)
        if not cur:
            raise TranslationError(f"Profil {name} gibt es nicht.")
        tr = profile_input(request.get_json(force=True) or {}, st["rulesets"])
        old = {k: v for k, v in cur["translate"].items() if k in TRANSLATE_KINDS}
        lines = profile_lines(name, tr, old)
        if len(lines) == 1:
            return jsonify(ok=True, error=None, config=[], name=name, unchanged=True)
        res = apply(lines)
        res["name"] = name
        verify_profile(res, name, tr)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.delete("/api/translation/profiles/<name>")
    def delete_profile(name):
        cur = next((p for p in load()["profiles"] if p["name"] == name), None)
        if not cur:
            raise TranslationError(f"Profil {name} gibt es nicht.")
        if cur["used"]:
            raise TranslationError(f"Profil {name} wird noch verwendet: " +
                                   ", ".join(f"{u['where']} {u['id']} ({u['dir']})" for u in cur["used"]) + ".")
        res = apply([f"no voice translation-profile {name}"])
        res["name"] = name
        return jsonify(res), (200 if res["ok"] else 400)

    # E.164-Pattern-Maps (e164.py) im selben Tab
    import e164
    e164.register(app, router, apply)
