"""
portal.py - Benutzerseite und Rechte fuer cme_web

Zwei Rollen:
  * Administratoren (lokaler Benutzer, OIDC-Gruppe aus CME_ADMIN_GROUPS): die bisherige Seite "/" mit allem
  * Benutzer: nur die Seite "/me" mit den eigenen Rufnummern:
      - Voicemail der eigenen Postfaecher anhoeren, als gehoert markieren, loeschen
      - Faxe senden (Absender: eigene Nummern) und die empfangenen Faxe der eigenen Faxnummern
      - Kurzwahltasten der eigenen Telefone
      - Telefonbuch (gemeinsam fuer alle): Namen zu Rufnummern eintragen
      - Gespraechsaufzeichnungen, an denen eine der eigenen Rufnummern beteiligt war (anhoeren, herunterladen)

Welche Rufnummern (Durchwahlen, Faxnummern) zu welchem Benutzer gehoeren, legt der Administrator fest
(Tab Benutzer). Ablage: <VM_CONFIG_DIR>/cme-users.json.

Die Benutzerseite nutzt fuer Aktionen die vorhandenen API-Aufrufe; guard() laesst fuer Benutzer nur diese
zu und prueft, dass Postfach, Fax oder Telefon zu den eigenen Rufnummern gehoert.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from flask import jsonify, redirect, request, send_file

BASE = Path(__file__).resolve().parent
RE_NUM = re.compile(r"^\+?[0-9*#]{1,32}$")


class PortalError(ValueError):
    pass


def register(app, router, demo: bool, cfgdir: Path) -> None:  # noqa: ARG001
    store = cfgdir / "cme-users.json"
    lock = threading.Lock()

    def load() -> dict:
        try:
            d = json.loads(store.read_text())
        except (FileNotFoundError, ValueError):
            d = {}
        d.setdefault("users", {})
        d.setdefault("seen", {})
        return d

    def save(d: dict) -> None:
        store.parent.mkdir(parents=True, exist_ok=True)
        tmp = store.with_name("." + store.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
        tmp.replace(store)
        # Adressen für Anrufe von draußen (phoneapp.write_sip_aliases) neu schreiben
        f = app.config.get("SIP_ALIASES")
        if f:
            f()

    def me() -> dict | None:
        auth = app.config.get("AUTH") or {}
        f = auth.get("whoami")
        return f() if f else {"admin": True, "key": "", "name": ""}

    def my_numbers(u: dict) -> list[str]:
        return list(load()["users"].get(u.get("key", ""), {}).get("numbers", []))

    # ---------------- was gehoert dem Benutzer ----------------

    def vm():
        return app.config.get("VOICEMAIL") or {}

    def fax():
        return app.config.get("FAX") or {}

    def pb():
        return app.config.get("PHONEBOOK") or {}

    def rec():
        return app.config.get("RECORDING") or {}

    def my_boxes(nums: list[str]) -> set[str]:
        v = vm()
        if not v:
            return set()
        boxes = v["settings"]()["mailboxes"]
        return {v["mbid"](n) for n in nums if n in boxes}

    def my_pools(nums: list[str]) -> list[dict]:
        try:
            pools = pb()["pools"]()
        except Exception:
            return []
        return [p for p in pools if set(p["lines"]) & set(nums)]

    def fax_meta(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    # ---------------- Rechte ----------------

    # Fuer Benutzer erlaubte vorhandene Aufrufe: (Methode, Muster, Pruefung)
    def chk_box(m, nums):
        return m.group(1) in my_boxes(nums)

    def chk_fax_in(m, nums):
        d = fax().get("faxdir")
        return bool(d) and fax_meta(d / f"{m.group(1)}.json").get("number") in nums

    def chk_fax_out(m, nums):
        d = fax().get("outdir")
        return bool(d) and fax_meta(d / f"{m.group(1)}.json").get("from") in nums

    def chk_fax_send(m, nums):  # noqa: ARG001
        return (request.form.get("from") or "").strip() in nums

    def chk_pool(m, nums):
        return any(p["tag"] == m.group(1) for p in my_pools(nums))

    def chk_rec(m, nums):
        r = rec()
        return bool(r) and r["may"](m.group(1), nums)

    def always(m, nums):  # noqa: ARG001
        return True

    RULES = [
        ("GET", r"/api/voicemail/audio/(\d+)/[A-Za-z]+/msg\d{4}", chk_box),
        ("DELETE", r"/api/voicemail/messages/(\d+)/[A-Za-z]+/msg\d{4}", chk_box),
        ("POST", r"/api/voicemail/messages/(\d+)/[A-Za-z]+/msg\d{4}/move", chk_box),
        ("GET", r"/api/vmapps/fax/files/([0-9A-Za-z._\-]{1,80})\.pdf", chk_fax_in),
        ("DELETE", r"/api/vmapps/fax/files/([0-9A-Za-z._\-]{1,80})", chk_fax_in),
        ("POST", r"/api/vmapps/fax/send", chk_fax_send),
        ("GET", r"/api/vmapps/fax/sent/([0-9a-f]{10,40})\.pdf", chk_fax_out),
        ("POST", r"/api/vmapps/fax/sent/([0-9a-f]{10,40})/retry", chk_fax_out),
        ("DELETE", r"/api/vmapps/fax/sent/([0-9a-f]{10,40})", chk_fax_out),
        ("GET", r"/api/recording/files/([0-9]{8}-[0-9]{6}-[0-9a-f]{6})", chk_rec),
        ("PUT", r"/api/phonebook/speeddials/(\d+)", chk_pool),
        ("POST", r"/api/phonebook/entries", always),
        ("PUT", r"/api/phonebook/entries/[0-9A-Za-z]{1,40}", always),
        ("DELETE", r"/api/phonebook/entries/[0-9A-Za-z]{1,40}", always),
    ]
    OPEN_PREFIX = ("/static/", "/login", "/logout", "/auth/", "/healthz", "/ipphone/")
    OPEN_EXACT = {"/me", "/api/me", "/api/user", "/api/auth/info"}

    def remember(u: dict) -> None:
        """Angemeldete Benutzer merken (Auswahlliste fuer die Zuordnung), hoechstens einmal pro Stunde."""
        key = u.get("key")
        if not key:
            return
        with lock:
            d = load()
            s = d["seen"].get(key, {})
            if time.time() - s.get("ts", 0) < 3600 and s.get("admin") == u.get("admin"):
                return
            d["seen"][key] = {"name": u.get("name", ""), "email": u.get("email", ""),
                              "provider": u.get("provider", ""), "admin": bool(u.get("admin")), "ts": int(time.time())}
            save(d)

    @app.before_request
    def guard():
        path = request.path
        if path.startswith(("/ipphone/", "/static/", "/healthz", "/login", "/auth/", "/client/")):
            return None
        u = me()
        if u is None:
            return None             # nicht angemeldet: auth.require_login hat schon umgeleitet
        try:
            remember(u)
        except Exception:
            pass
        if u.get("admin"):
            return None
        if path == "/":
            return redirect("/me")
        if path.startswith(OPEN_PREFIX) or path in OPEN_EXACT:
            return None
        nums = my_numbers(u)
        for method, rx, check in RULES:
            m = re.fullmatch(rx, path)
            if m and request.method == method:
                if check(m, nums):
                    return None
                break
        if path.startswith("/api/"):
            return jsonify(ok=False, error="Keine Berechtigung (nur für Administratoren)."), 403
        return redirect("/me")

    @app.errorhandler(PortalError)
    def on_err(e):
        return jsonify(ok=False, error=str(e)), 400

    # ---------------- Benutzerseite ----------------

    @app.get("/static/user.js")
    def user_js():
        return send_file(BASE / "static" / "user.js", mimetype="text/javascript", max_age=0)

    @app.get("/static/users.js")
    def users_js():
        return send_file(BASE / "static" / "users.js", mimetype="text/javascript", max_age=0)

    @app.get("/me")
    def user_page():
        # gleiche Gestaltung wie die Administration: <head> (Stile) aus index.html uebernehmen
        html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
        head = html.split("</head>", 1)[0].replace("<title>CME Telefonanlage</title>", "<title>Telefon</title>")
        body = (BASE / "static" / "user.html").read_text(encoding="utf-8")
        return head + "</head>\n" + body, 200, {"Content-Type": "text/html; charset=utf-8",
                                                "Cache-Control": "no-cache"}

    @app.get("/api/user")
    def user_data():
        u = me() or {}
        nums = my_numbers(u)
        out = {"ok": True, "user": {k: u.get(k) for k in ("name", "email", "provider", "admin")}, "numbers": [],
               "voicemail": [], "fax": {"enabled": False}, "pools": [], "phonebook": [], "recordings": None}
        v = vm()
        dns = {}
        try:
            dns = {n["number"]: n for n in v["numbers"]()} if v else {}
        except Exception as e:
            out["error"] = f"Router: {type(e).__name__}: {e}"
        f = fax()
        faxnums = {x["number"]: x for x in (f["numbers"]() if f else [])}
        for n in nums:
            kind = "fax" if n in faxnums else ("phone" if n in dns else "unknown")
            name = (dns.get(n) or {}).get("name") or (faxnums.get(n) or {}).get("name", "")
            out["numbers"].append({"number": n, "name": name, "kind": kind})
        if v and v.get("enabled"):
            boxes = v["settings"]()["mailboxes"]
            for n in nums:
                if n in boxes:
                    msgs = v["messages"](v["mbid"](n))
                    out["voicemail"].append({"number": n, "id": v["mbid"](n), "name": (dns.get(n) or {}).get("name", ""),
                                             "email": boxes[n].get("email", ""), "messages": msgs,
                                             "new": sum(m["folder"] == "INBOX" for m in msgs)})
        if f and f.get("enabled"):
            mine = set(nums)
            out["fax"] = {"enabled": True,
                          "numbers": [x for x in faxnums.values() if x["number"] in mine],
                          "received": [r for r in f["received"]() if r.get("number") in mine],
                          "sent": [j for j in f["sent"]() if j.get("from") in mine],
                          "senders": [{"number": n["number"], "name": n["name"]} for n in out["numbers"]
                                      if n["kind"] in ("fax", "phone")]}
        out["pools"] = [{"tag": p["tag"], "desc": p["desc"], "type": p["type"], "lines": p["lines"], "speed": p["speed"]}
                        for p in my_pools(nums)]
        r = rec()
        if r and r["enabled"]():
            try:
                out["recordings"] = r["list"](nums)
            except Exception as e:
                out["recordings"] = []
                out["error"] = f"Aufzeichnungen: {type(e).__name__}: {e}"
        if pb().get("entries"):
            out["phonebook"] = sorted(pb()["entries"](), key=lambda e: (bool(e.get("name")), -e.get("last", 0)))
        return jsonify(out)

    # ---------------- Administration: Zuordnung Benutzer -> Rufnummern ----------------

    @app.get("/api/users")
    def users_get():
        d = load()
        v, f = vm(), fax()
        try:
            dns = [{"number": n["number"], "name": n["name"], "kind": "phone"} for n in (v["numbers"]() if v else [])]
        except Exception:
            dns = []
        faxes = [{"number": x["number"], "name": x.get("name", ""), "kind": "fax"} for x in (f["numbers"]() if f else [])]
        keys = sorted(set(d["users"]) | set(d["seen"]))
        users = [{"key": k, "numbers": d["users"].get(k, {}).get("numbers", []), **d["seen"].get(k, {})} for k in keys]
        return jsonify(ok=True, users=users, numbers=dns + faxes, me=(me() or {}).get("key", ""))

    @app.put("/api/users/<path:key>")
    def users_put(key):
        key = key.strip().lower()
        if not key or len(key) > 200:
            raise PortalError("Ungültiger Benutzer.")
        b = request.get_json(force=True) or {}
        nums = []
        for n in b.get("numbers") or []:
            n = str(n).strip()
            if not RE_NUM.match(n):
                raise PortalError(f"Ungültige Rufnummer: {n}")
            if n not in nums:
                nums.append(n)
        with lock:
            d = load()
            if nums:
                d["users"][key] = {"numbers": nums}
            else:
                d["users"].pop(key, None)
            save(d)
        return jsonify(ok=True)

    @app.delete("/api/users/<path:key>")
    def users_delete(key):
        key = key.strip().lower()
        with lock:
            d = load()
            d["users"].pop(key, None)
            d["seen"].pop(key, None)
            save(d)
        return jsonify(ok=True)
