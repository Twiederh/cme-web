"""
locales.py - Sprachpakete fuer die Telefone (Tab "Telefone & Rufnummern")

Das Locale-Paket von Cisco (z. B. CME-locale-de_DE-German-8.6.3.0.tar) wird unveraendert per SCP nach
<CME_MEDIA_DIR>/ (Standard bootflash:/its) kopiert und vom Locale-Installer des Routers ausgepackt:

  voice register global
   user-locale <tag> DE load bootflash:/its/CME-locale-de_DE-....tar
   network-locale <tag> DE
   create profile
  voice register pool <n> / reset          (optional)

Tag 0 ist die Standardsprache aller Telefone. Hochladen laeuft im Hintergrund wie bei der Firmware.
Wird aus features.register() eingehaengt; Oberflaeche: static/locales.js.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, request, send_file

import media

BASE = Path(__file__).resolve().parent
LOCALE_DIR = media.DEFAULT_BASE
MAX_UPLOAD = 300 * 1024 * 1024
RE_TAR = re.compile(r"^CME-locale-([a-z]{2})_([A-Z]{2})-[A-Za-z0-9_.\-]+\.tar$")
RE_CODE = re.compile(r"^[A-Z]{2}$")
RE_LINE = re.compile(r"^ (user|network)-locale\s+(?:(\d)\s+)?(?:(U\d)\s+)?([A-Z]{2})(?:\s+load\s+(\S+))?\s*$")


class LocaleError(ValueError):
    pass


def parse_name(filename: str) -> dict:
    name = posixpath.basename(filename)
    m = RE_TAR.match(name)
    if not m:
        raise LocaleError("Erwartet wird das Locale-Paket von Cisco, z. B. CME-locale-de_DE-German-8.6.3.0.tar.")
    return {"file": name, "lang": m.group(1), "code": m.group(2)}


def parse_locales(glob: str) -> list[dict]:
    """voice register global -> [{kind, tag, code, load, line}]"""
    out = []
    for line in glob.splitlines():
        m = RE_LINE.match(line)
        if m:
            out.append({"kind": m.group(1), "tag": m.group(2) or "0", "ud": m.group(3) or "",
                        "code": m.group(4), "load": m.group(5) or "", "line": line.strip()})
    return out


def register(app, router, apply, demo: bool) -> None:
    raw = getattr(router, "router", router)          # SCP braucht die echte Verbindung, nicht den Cache
    files = media.DemoFiles(router) if demo else media.RouterFiles(raw)
    app.config["MAX_CONTENT_LENGTH"] = max(app.config.get("MAX_CONTENT_LENGTH") or 0, MAX_UPLOAD)
    job: dict = {}
    job_lock = threading.Lock()

    def state() -> dict:
        glob = router.show("show running-config | section voice register global")
        pools = router.show("show running-config | section voice register pool")
        try:
            listing = files.list_dir(LOCALE_DIR)
        except Exception:
            listing = {}
        locs = parse_locales(glob)
        used = {posixpath.basename(l["load"]) for l in locs if l["load"]}
        tars = [{"file": n, "size": s, "path": f"{LOCALE_DIR}/{n}", "code": RE_TAR.match(n).group(2),
                 "used": n in used} for n, s in sorted(listing.items()) if RE_TAR.match(n)]
        return {"tars": tars, "locales": locs, "dir": LOCALE_DIR,
                "pools": re.findall(r"^voice register pool\s+(\d+)", pools, re.M)}

    def install(path: str, code: str, tag: str, network: bool, reset: bool) -> dict:
        st = state()
        lines = ["voice register global", f" user-locale {tag} {code} load {path}"]
        if network:
            lines.append(f" network-locale {tag} {code}")
        lines.append(" create profile")
        if reset:
            for p in st["pools"]:
                lines += [f"voice register pool {p}", " reset"]
        res = apply(lines)
        out = res.get("output") or ""
        bad = [l.strip() for l in out.splitlines() if re.search(r"LOCALE (ERROR|INSTALLER MESSAGE)", l)]
        if res.get("ok") and any("ERROR" in l for l in bad):
            res.update(ok=False, error="\n".join(bad))
        if res.get("ok") and not demo:          # der Demo-Router merkt sich voice register global nicht
            now = parse_locales(router.show("show running-config | section voice register global"))
            if not any(l["kind"] == "user" and l["tag"] == tag and l["code"] == code for l in now):
                res.update(ok=False, error="Der Router hat die Sprache nicht übernommen.\n\n"
                           + "\n".join([l for l in out.splitlines() if l.strip()][-20:]))
        if bad and res.get("ok"):
            res["note"] = "\n".join(bad)
        res.pop("output", None)
        return res

    # ---------------- Hochladen (Hintergrund) ----------------

    def run_job(tpath: str, info: dict, opts: dict, jid: str) -> None:
        def upd(**kw):
            with job_lock:
                if job.get("id") == jid:
                    job.update(kw)
        try:
            data = Path(tpath).read_bytes()
            dest = f"{LOCALE_DIR}/{info['file']}"
            upd(total=len(data), done=0)
            if files.list_dir(LOCALE_DIR).get(info["file"]) == len(data):
                upd(step=f"{info['file']} ist schon im Flash", done=len(data))
            else:
                upd(step=f"Lade {info['file']} ({len(data) // 1024} KB) in den Flash …")
                files.mkdirs(LOCALE_DIR)
                files.upload(data, dest)
                upd(done=len(data))
            if opts.get("install"):
                upd(step="Locale-Installer des Routers läuft …")
                res = install(dest, info["code"], opts["tag"], opts["network"], opts["reset"])
                if not res.get("ok"):
                    raise LocaleError(res.get("error") or "Installation fehlgeschlagen")
                upd(state="done", step=f"Installiert: {info['code']} (user-locale {opts['tag']})", ts_end=time.time(),
                    note=res.get("note", ""))
            else:
                upd(state="done", step=f"Hochgeladen: {dest}", ts_end=time.time())
        except Exception as e:
            msg = str(e) if isinstance(e, (LocaleError, media.MediaError)) else f"{type(e).__name__}: {e}"
            upd(state="error", step="Abgebrochen", error=msg, ts_end=time.time())
        finally:
            try:
                os.unlink(tpath)
            except OSError:
                pass

    def options(b) -> dict:
        tag = str(b.get("tag") or "0").strip()
        if not re.fullmatch(r"[0-4]", tag):
            raise LocaleError("Tag: 0 bis 4 (0 = Standard für alle Telefone).")
        return {"tag": tag, "network": str(b.get("network", "1")).lower() in ("1", "true", "on", "yes"),
                "reset": str(b.get("reset", "")).lower() in ("1", "true", "on", "yes"),
                "install": str(b.get("install", "1")).lower() in ("1", "true", "on", "yes")}

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
        opts = options(request.form)
        with job_lock:
            if job.get("state") == "running":
                raise LocaleError("Es läuft schon ein Hochladen.")
        fd, tpath = tempfile.mkstemp(suffix=".tar")
        os.close(fd)
        f.save(tpath)
        jid = uuid.uuid4().hex[:8]
        with job_lock:
            job.clear()
            job.update(id=jid, state="running", step="Übertrage …", file=info["file"], total=0, done=0,
                       ts_start=time.time())
        threading.Thread(target=run_job, args=(tpath, info, opts, jid), daemon=True).start()
        return jsonify(ok=True, job=dict(job))

    @app.post("/api/locales/install")
    def loc_install():
        """Body: {file, tag, network, reset} - schon im Flash liegendes Paket installieren"""
        b = request.get_json(force=True) or {}
        info = parse_name(str(b.get("file") or ""))
        opts = options(b)
        st = state()
        tar = next((t for t in st["tars"] if t["file"] == info["file"]), None)
        if not tar:
            raise LocaleError(f"{info['file']} liegt nicht in {LOCALE_DIR}.")
        res = install(tar["path"], info["code"], opts["tag"], opts["network"], opts["reset"])
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/locales/remove")
    def loc_remove():
        """Body: {kind, tag, reset} - Sprache/Toene fuer einen Tag zuruecknehmen (Englisch/USA)"""
        b = request.get_json(force=True) or {}
        kind, tag = str(b.get("kind") or ""), str(b.get("tag") or "")
        st = state()
        cur = next((l for l in st["locales"] if l["kind"] == kind and l["tag"] == tag), None)
        if not cur:
            raise LocaleError("Diese Einstellung gibt es nicht.")
        ud = f"{cur['ud']} " if cur["ud"] else ""
        lines = ["voice register global", f" no {kind}-locale {cur['tag']} {ud}{cur['code']}", " create profile"]
        if b.get("reset"):
            for p in st["pools"]:
                lines += [f"voice register pool {p}", " reset"]
        res = apply(lines)
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.delete("/api/locales/<name>")
    def loc_delete(name):
        info = parse_name(name)
        st = state()
        tar = next((t for t in st["tars"] if t["file"] == info["file"]), None)
        if not tar:
            raise LocaleError("Datei nicht gefunden.")
        if tar["used"]:
            raise LocaleError("Das Paket ist noch per user-locale … load eingetragen – zuerst die Sprache entfernen.")
        files.delete(tar["path"])
        return jsonify(ok=True)
