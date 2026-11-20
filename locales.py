"""
locales.py - Sprachpakete fuer die Telefone (Tab "Telefone & Rufnummern")

Nimmt das Locale-Paket von Cisco an (z. B. CME-locale-de_DE-German_Germany-14.1.14.1.tar), laedt die
Dateien per SCP nach <CME_MEDIA_DIR>/locale/<Sprache>/ und setzt fuer jede Datei einen tftp-server-Alias:

  Dateien in Unterordnern des Pakets   -> Alias = Pfad im Paket (z. B. German_Germany/...)
  lose Dateien *-tones.xml             -> <Land>/<datei>          (Netz-Locale: Toene, z. B. Germany/)
  andere lose Dateien                  -> <Sprache_Land>/<datei>  (User-Locale: Texte, z. B. German_Germany/)

Danach laesst sich die Sprache fuer alle SIP-Telefone einschalten:

  voice register global / user-locale DE / network-locale DE / create profile   (+ optional reset)

Hochladen laeuft im Hintergrund wie bei der Firmware. Wird aus features.register() eingehaengt;
Oberflaeche: static/locales.js.
"""

from __future__ import annotations

import os
import posixpath
import re
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, request, send_file

import media

BASE = Path(__file__).resolve().parent
LOCALE_DIR = f"{media.DEFAULT_BASE}/locale"
MAX_UPLOAD = 300 * 1024 * 1024
RE_TAR = re.compile(r"^CME-locale-([a-z]{2})_([A-Z]{2})-([A-Za-z]+(?:_[A-Za-z]+)*)-([0-9.]+)\.tar$")
RE_PART = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
RE_LANGDIR = re.compile(r"^[A-Z][a-z]+_[A-Z][A-Za-z_]+$")
RE_CODE = re.compile(r"^[A-Z]{2}$")


class LocaleError(ValueError):
    pass


def parse_name(filename: str) -> dict:
    m = RE_TAR.match(posixpath.basename(filename))
    if not m:
        raise LocaleError("Erwartet wird das Locale-Paket von Cisco, z. B. CME-locale-de_DE-German_Germany-14.1.14.1.tar.")
    lang, cc, name, version = m.groups()
    if "_" not in name:                      # aeltere Pakete: CME-locale-de_DE-German-8.6.tar
        name = f"{name}_{_country_name(cc)}"
    return {"lang": lang, "code": cc, "name": name, "country": name.split("_", 1)[1], "version": version,
            "id": f"{name}-{version}"}


def parse_locales(glob: str) -> dict:
    """voice register global -> {user: code, network: code}"""
    res = {"user": "", "network": ""}
    for line in glob.splitlines():
        m = re.match(r"^ (user|network)-locale\s+(?:\d+\s+)?(?:U\d\s+)?([A-Z]{2})\b", line)
        if m:
            res[m.group(1)] = m.group(2)
    return res


def register(app, router, apply, demo: bool) -> None:
    raw = getattr(router, "router", router)          # SCP braucht die echte Verbindung, nicht den Cache
    files = media.DemoFiles(router) if demo else media.RouterFiles(raw)
    app.config["MAX_CONTENT_LENGTH"] = max(app.config.get("MAX_CONTENT_LENGTH") or 0, MAX_UPLOAD)
    job: dict = {}
    job_lock = threading.Lock()

    def aliases() -> dict[str, str]:
        return media.parse_tftp_aliases(router.show("show running-config | include ^tftp-server"))

    def installed(al: dict[str, str]) -> list[dict]:
        """Pakete in <LOCALE_DIR>/<id>/ anhand der tftp-Aliase"""
        out: dict[str, dict] = {}
        for alias, path in sorted(al.items()):
            if not path.startswith(LOCALE_DIR + "/"):
                continue
            pid = path[len(LOCALE_DIR) + 1:].split("/", 1)[0]
            e = out.setdefault(pid, {"id": pid, "files": [], "dirs": set()})
            e["files"].append({"alias": alias, "path": path})
            if "/" in alias:
                e["dirs"].add(alias.split("/", 1)[0])
        res = []
        for pid, e in sorted(out.items()):
            m = re.match(r"^([A-Za-z]+_[A-Za-z_]+)-([0-9.]+)$", pid)
            res.append({"id": pid, "name": m.group(1) if m else pid, "version": m.group(2) if m else "",
                        "count": len(e["files"]), "dirs": sorted(e["dirs"]), "files": e["files"]})
        return res

    def state() -> dict:
        al = aliases()
        glob = router.show("show running-config | section voice register global")
        pools = router.show("show running-config | section voice register pool")
        return {"locales": installed(al), "current": parse_locales(glob), "dir": LOCALE_DIR,
                "pools": re.findall(r"^voice register pool\s+(\d+)", pools, re.M)}

    # ---------------- Hochladen (Hintergrund) ----------------

    def run_job(tpath: str, info: dict, jid: str) -> None:
        def upd(**kw):
            with job_lock:
                if job.get("id") == jid:
                    job.update(kw)
        try:
            with tarfile.open(tpath) as t:
                members = [m for m in t.getmembers() if m.isfile()]
                plan = []                      # (member, relativer Pfad, Alias)
                names = [posixpath.normpath(m.name.lstrip("./")) for m in members]
                # gemeinsamen Oberordner (z. B. de_DE/) weglassen, wenn er kein Locale-Ordner ist
                tops = {n.split("/", 1)[0] for n in names}
                strip = ""
                if len(tops) == 1 and all("/" in n for n in names):
                    top = tops.pop()
                    if not RE_LANGDIR.match(top) and top != info["country"]:
                        strip = top + "/"
                for m, rel in zip(members, names):
                    rel = rel[len(strip):]
                    parts = rel.split("/")
                    if rel.startswith("..") or any(not RE_PART.match(p) for p in parts) or len(parts) > 4:
                        raise LocaleError(f"Unerwarteter Dateiname im Paket: {m.name}")
                    if len(parts) > 1:
                        alias = rel
                    elif parts[0].endswith("-tones.xml"):
                        alias = f"{info['country']}/{parts[0]}"
                    else:
                        alias = f"{info['name']}/{parts[0]}"
                    plan.append((m, rel, alias))
                if not plan:
                    raise LocaleError("Das Paket ist leer.")
                dup = {a for _, _, a in plan if sum(1 for _, _, b in plan if b == a) > 1}
                if dup:
                    raise LocaleError(f"Doppelte Dateien im Paket: {', '.join(sorted(dup)[:5])}")
                total = sum(m.size for m, _, _ in plan)
                base = f"{LOCALE_DIR}/{info['id']}"
                upd(step="Ordner anlegen", total=total, done=0, files=len(plan))
                done = 0
                made: set[str] = set()
                listing: dict[str, dict[str, int]] = {}
                for m, rel, alias in sorted(plan, key=lambda x: x[1]):
                    dest = f"{base}/{rel}"
                    d, n = dest.rsplit("/", 1)
                    if d not in made:
                        files.mkdirs(d)
                        made.add(d)
                        listing[d] = files.list_dir(d)
                    if listing[d].get(n) == m.size:
                        done += m.size
                        upd(step=f"{rel} ist schon da", done=done)
                        continue
                    upd(step=f"Lade {rel} ({max(1, m.size // 1024)} KB) …")
                    files.upload(t.extractfile(m).read(), dest)
                    done += m.size
                    upd(done=done)
            upd(step="tftp-server-Einträge setzen")
            al = aliases()
            cfg = []
            for _, rel, alias in plan:
                path = f"{base}/{rel}"
                old = al.get(alias)
                if old == path:
                    continue
                if old:
                    cfg.append(f"no tftp-server {old} alias {alias}")
                cfg.append(f"tftp-server {path} alias {alias}")
            res = apply(cfg) if cfg else {"ok": True}
            if not res.get("ok"):
                raise LocaleError(f"tftp-server-Einträge: {res.get('error')}")
            upd(state="done", step=f"Fertig: {info['name']} {info['version']} ({len(plan)} Dateien)",
                ts_end=time.time())
        except Exception as e:
            msg = str(e) if isinstance(e, (LocaleError, media.MediaError, tarfile.TarError)) \
                else f"{type(e).__name__}: {e}"
            upd(state="error", step="Abgebrochen", error=msg, ts_end=time.time())
        finally:
            try:
                os.unlink(tpath)
            except OSError:
                pass

    # ---------------- API ----------------

    @app.errorhandler(LocaleError)
    def on_locale_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/locales.js")
    def locales_js():
        return send_file(BASE / "static" / "locales.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/locales")
    def loc_list():
        st = state()
        with job_lock:
            j = dict(job)
        return jsonify(ok=True, job=j, **st)

    @app.get("/api/locales/job")
    def loc_job():
        with job_lock:
            return jsonify(ok=True, job=dict(job))

    @app.post("/api/locales/upload")
    def loc_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            raise LocaleError("Keine Datei.")
        info = parse_name(f.filename)
        with job_lock:
            if job.get("state") == "running":
                raise LocaleError("Es läuft schon ein Hochladen.")
        fd, tpath = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        f.save(tpath)
        if not tarfile.is_tarfile(tpath):
            os.unlink(tpath)
            raise LocaleError("Die Datei ist kein gültiges TAR-Archiv.")
        jid = uuid.uuid4().hex[:8]
        with job_lock:
            job.clear()
            job.update(id=jid, state="running", step="Paket prüfen", file=f.filename, total=0, done=0,
                       ts_start=time.time(), locale=info)
        threading.Thread(target=run_job, args=(tpath, info, jid), daemon=True).start()
        return jsonify(ok=True, job=dict(job))

    @app.post("/api/locales/activate")
    def loc_activate():
        """Body: {user: "DE"|"", network: "DE"|"", reset: bool}"""
        b = request.get_json(force=True) or {}
        user, net = str(b.get("user") or "").strip().upper(), str(b.get("network") or "").strip().upper()
        for c in (user, net):
            if c and not RE_CODE.match(c):
                raise LocaleError("Ländercode: zwei Buchstaben, z. B. DE.")
        st = state()
        cur = st["current"]
        lines = ["voice register global"]
        for kind, want in (("user", user), ("network", net)):
            if want == cur[kind]:
                continue
            if want:
                lines.append(f" {kind}-locale {want}")
            else:
                lines.append(f" no {kind}-locale {cur[kind]}")
        if len(lines) == 1:
            return jsonify(ok=True, config=[], unchanged=True)
        lines.append(" create profile")
        if b.get("reset"):
            for p in st["pools"]:
                lines += [f"voice register pool {p}", " reset"]
        res = apply(lines)
        if res.get("ok"):
            now = parse_locales(router.show("show running-config | section voice register global"))
            if (now["user"], now["network"]) != (user, net):
                res.update(ok=False, error="Der Router hat die Sprache nicht übernommen.\n\n"
                           + "\n".join([l for l in (res.get("output") or "").splitlines() if l.strip()][-20:]))
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.delete("/api/locales/<pid>")
    def loc_delete(pid):
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,80}", pid):
            raise LocaleError("Ungültiges Paket.")
        st = state()
        loc = next((l for l in st["locales"] if l["id"] == pid), None)
        if not loc:
            raise LocaleError(f"Paket {pid} nicht gefunden.")
        in_use = [k for k in ("user", "network")
                  if st["current"][k] and loc["name"].endswith("_" + _country_name(st["current"][k]))]
        if in_use and len([l for l in st["locales"] if l["name"] == loc["name"]]) == 1:
            raise LocaleError("Die Sprache ist noch eingeschaltet – zuerst auf Standard zurückstellen.")
        cfg = [f"no tftp-server {f['path']} alias {f['alias']}" for f in loc["files"]]
        res = apply(cfg) if cfg else {"ok": True, "error": None, "config": []}
        if res.get("ok"):
            d = f"{LOCALE_DIR}/{pid}"
            if demo:
                for p in [p for p in router.files if p.startswith(d + "/")]:
                    router.files.pop(p, None)
            else:
                raw._do(lambda c: c.send_command_timing(f"delete /force /recursive {d}", read_timeout=120))
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)


def _country_name(code: str) -> str:
    return {"DE": "Germany", "AT": "Austria", "CH": "Switzerland", "FR": "France", "IT": "Italy",
            "ES": "Spain", "NL": "Netherlands", "GB": "United_Kingdom", "US": "United_States"}.get(code, code)
