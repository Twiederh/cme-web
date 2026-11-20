"""
backup.py - Sicherung und Wiederherstellung der CME-Konfiguration (Tab "Sicherung")

Sicherung (ZIP):
  manifest.json      Router, Zeitpunkt, Dateiliste (Originalpfad, Groesse)
  cme-config.txt     die CME-relevanten Teile der running-config (voice, dial-peer, sip-ua, telephony-service,
                     voice register, tftp-server, Uebersetzungen, Uhrzeit-Zone ...)
  files/...          optional alle Dateien im Flash, auf die diese Konfiguration verweist
                     (tftp-server: Firmware, Klingeltoene, Hintergruende; user-locale ... load; moh ...)

Wiederherstellung auf einem (neuen) Router:
  1. Sicherung hochladen -> Vorschau mit gefundenen IP-Adressen und Speicherorten (flash:/bootflash:),
     die man fuer den neuen Router ersetzen kann
  2. Dateien per SCP in den Flash, dann die Konfiguration blockweise einspielen, zum Schluss
     voice register global / create profile (und telephony-service / create cnf-files bei SCCP)
  3. optional write memory

Laeuft beides im Hintergrund (Firmware-Dateien sind gross). Daten von cme-web selbst (Asterisk, Benutzer,
Telefonbuch) liegen im Docker-Volume und gehoeren nicht zum Router.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import jsonify, request, send_file

import media

BASE = Path(__file__).resolve().parent
MAX_RESTORE = 3 * 1024 * 1024 * 1024
# Bloecke/Zeilen der running-config, die zu CME gehoeren
INCLUDE = ("voice service", "voice class", "voice translation-rule", "voice translation-profile", "voice register",
           "voice hunt-group", "voice logout-profile", "voice user-profile", "voice-card", "dial-peer", "sip-ua",
           "telephony-service", "ephone", "tftp-server", "application", "trunk group", "stcapp", "sccp",
           "dspfarm", "clock timezone", "clock summer-time", "ip scp server enable")
SKIP_LINE = re.compile(r"^\s*(create profile|create cnf-files)\b")
RE_FILE = re.compile(r"\b((?:bootflash|flash\d?|slot\d|usbflash\d):/?[^\s\"']+)")
RE_IP = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
RE_ID = re.compile(r"^[0-9a-f]{12}$")
CHUNK = 60


class BackupError(ValueError):
    pass


def cme_sections(run: str) -> list[list[str]]:
    """running-config -> CME-Bloecke (je Block die Kopfzeile und die eingerueckten Zeilen)"""
    blocks, cur = [], None
    for line in run.splitlines():
        if not line.strip() or line.startswith("!"):
            cur = None
            continue
        if not line.startswith(" "):
            cur = [line] if line.startswith(INCLUDE) else None
            if cur:
                blocks.append(cur)
            continue
        if cur is not None and not SKIP_LINE.match(line):
            cur.append(line.rstrip())
    return blocks


def referenced_files(text: str) -> list[str]:
    out = []
    for m in RE_FILE.finditer(text):
        p = m.group(1).rstrip(",;")
        fs, _, rest = p.partition(":")
        p = f"{fs}:/{rest.lstrip('/')}"
        if rest and not rest.endswith("/") and p not in out:
            out.append(p)
    return out


def suggestions(text: str, paths: list[str]) -> list[dict]:
    """IP-Adressen und Speicherorte, die auf dem neuen Router vermutlich anders sind"""
    ips: dict[str, list[str]] = {}
    for line in text.splitlines():
        for ip in RE_IP.findall(line):
            if ip.startswith(("0.", "255.")) or ip == "0.0.0.0":
                continue
            ips.setdefault(ip, [])
            if len(ips[ip]) < 3:
                ips[ip].append(line.strip())
    fss = sorted({p.split(":", 1)[0] + ":" for p in paths} |
                 {m.group(1).split(":", 1)[0] + ":" for m in RE_FILE.finditer(text)})
    out = [{"kind": "ip", "old": ip, "new": ip, "where": w} for ip, w in sorted(ips.items())]
    out += [{"kind": "fs", "old": fs, "new": fs, "where": []} for fs in fss]
    iface = sorted(set(re.findall(r"\b(?:source-interface|bind \w+ source-interface)\s+(\S+)", text)))
    out += [{"kind": "if", "old": i, "new": i, "where": []} for i in iface]
    return out


def replace_all(text: str, rep: list[tuple[str, str, str]]) -> str:
    for kind, old, new in rep:
        if not old or old == new:
            continue
        if kind == "ip":
            text = re.sub(rf"(?<![\d.]){re.escape(old)}(?![\d.])", new, text)
        elif kind == "if":
            text = re.sub(rf"(?<!\S){re.escape(old)}(?!\S)", new, text)
        else:
            text = text.replace(old, new)
    return text


def register(app, router, apply, demo: bool) -> None:
    raw = getattr(router, "router", router)
    files = media.DemoFiles(router) if demo else media.RouterFiles(raw)
    app.config["MAX_CONTENT_LENGTH"] = max(app.config.get("MAX_CONTENT_LENGTH") or 0, MAX_RESTORE)
    work = Path(tempfile.gettempdir()) / "cme-backup"
    work.mkdir(parents=True, exist_ok=True)
    job: dict = {}
    job_lock = threading.Lock()

    def live(cmd: str) -> str:
        if demo and cmd == "show running-config":
            return "hostname cme-demo\n!\n" + "\n".join(router.cfg) + "\n!\n" + router.dialpeers + "\n".join(router.tftp) + "\n"
        return getattr(router, "show_live", router.show)(cmd)

    def fetch(path: str, local: Path) -> None:
        """Datei vom Router holen (SCP)"""
        if demo:
            local.write_bytes(router.files.get(path, b""))
            return
        from netmiko import SCPConn
        for attempt in (1, 2, 3):
            try:
                def get(c):
                    scp = SCPConn(c, socket_timeout=300.0)
                    try:
                        scp.scp_get_file(path, str(local))
                    finally:
                        try:
                            scp.close()
                        except Exception:
                            pass
                raw._do(get)
                return
            except Exception as e:
                if attempt == 3:
                    raise BackupError(f"{path} konnte nicht geladen werden ({type(e).__name__}: {e}). "
                                      "Ist 'ip scp server enable' gesetzt?")
                time.sleep(2 * attempt)
                try:
                    if raw.conn:
                        raw.conn.disconnect()
                except Exception:
                    pass
                raw.conn = None

    def sizes(paths: list[str]) -> dict[str, int | None]:
        out, dirs = {}, {}
        for p in paths:
            d, n = p.rsplit("/", 1) if "/" in p.split(":", 1)[1] else (p.split(":", 1)[0] + ":", p.split(":", 1)[1])
            if d not in dirs:
                try:
                    dirs[d] = files.list_dir(d)
                except Exception:
                    dirs[d] = {}
            out[p] = dirs[d].get(n)
        return out

    def start(kind: str, **kw) -> str:
        with job_lock:
            if job.get("state") == "running":
                raise BackupError("Es läuft schon eine Sicherung oder Wiederherstellung.")
            jid = uuid.uuid4().hex[:12]
            job.clear()
            job.update(id=jid, kind=kind, state="running", step="Vorbereiten …", total=0, done=0,
                       ts_start=time.time(), log=[], **kw)
        return jid

    def upd(jid: str, **kw):
        with job_lock:
            if job.get("id") == jid:
                job.update(kw)

    def log(jid: str, text: str):
        with job_lock:
            if job.get("id") == jid:
                job["log"] = (job.get("log") or [])[-200:] + [text]

    # ---------------- Sicherung ----------------

    def run_backup(jid: str, with_files: bool) -> None:
        tmp = work / f"{jid}.part"
        try:
            run = live("show running-config")
            if "voice register" not in run and "telephony-service" not in run and "dial-peer" not in run:
                raise BackupError("In der running-config ist keine CME-Konfiguration zu finden.")
            blocks = cme_sections(run)
            text = "\n".join("\n".join(b) for b in blocks) + "\n"
            host = (re.search(r"^hostname (\S+)", run, re.M) or [None, ""])[1]
            version = (re.search(r"Version ([^\s,]+)", live("show version | include Version")) or [None, ""])[1]
            paths = referenced_files(text) if with_files else []
            sz = sizes(paths) if paths else {}
            missing = [p for p in paths if sz.get(p) is None]
            paths = [p for p in paths if sz.get(p) is not None]
            total = sum(sz[p] for p in paths)
            upd(jid, total=total, done=0, step=f"{len(blocks)} Konfigurationsblöcke, {len(paths)} Dateien")
            manifest = {"format": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "hostname": host,
                        "version": version, "blocks": len(blocks), "files": [], "missing": missing}
            done = 0
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr("cme-config.txt", text)
                for i, p in enumerate(paths):
                    upd(jid, step=f"Lade {p} ({(sz[p] or 0) // 1024} KB) …")
                    local = work / f"{jid}.f"
                    fetch(p, local)
                    arc = "files/" + p.replace(":", "", 1).lstrip("/")
                    z.write(local, arc, compress_type=zipfile.ZIP_STORED if p.endswith((".sbn", ".tar", ".zip", ".png", ".jpg"))
                            else zipfile.ZIP_DEFLATED)
                    manifest["files"].append({"path": p, "arc": arc, "size": local.stat().st_size})
                    local.unlink(missing_ok=True)
                    done += sz[p] or 0
                    upd(jid, done=done)
                z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            name = f"cme-backup-{host or 'router'}-{time.strftime('%Y%m%d-%H%M')}.zip"
            final = work / f"{jid}.zip"
            tmp.replace(final)
            for old in work.glob("*.zip"):          # nur die letzten Sicherungen aufheben
                if old != final and time.time() - old.stat().st_mtime > 6 * 3600:
                    old.unlink(missing_ok=True)
            upd(jid, state="done", step=f"Fertig: {len(blocks)} Blöcke, {len(paths)} Dateien", download=jid,
                name=name, size=final.stat().st_size, missing=missing, ts_end=time.time())
        except Exception as e:
            tmp.unlink(missing_ok=True)
            msg = str(e) if isinstance(e, (BackupError, media.MediaError)) else f"{type(e).__name__}: {e}"
            upd(jid, state="error", step="Abgebrochen", error=msg, ts_end=time.time())

    # ---------------- Wiederherstellung ----------------

    def open_restore(rid: str) -> tuple[zipfile.ZipFile, dict, str]:
        if not RE_ID.match(rid):
            raise BackupError("Ungültige Sicherung.")
        p = work / f"restore-{rid}.zip"
        if not p.exists():
            raise BackupError("Die hochgeladene Sicherung ist nicht mehr da – bitte erneut hochladen.")
        z = zipfile.ZipFile(p)
        try:
            manifest = json.loads(z.read("manifest.json"))
            text = z.read("cme-config.txt").decode("utf-8", "replace")
        except KeyError:
            z.close()
            raise BackupError("Das ist keine Sicherung von cme-web (manifest.json fehlt).")
        return z, manifest, text

    def chunks(text: str) -> list[list[str]]:
        blocks, cur = [], []
        for line in text.splitlines():
            if not line.strip():
                continue
            if not line.startswith(" ") and cur:
                blocks.append(cur)
                cur = []
            cur.append(line)
        if cur:
            blocks.append(cur)
        out, part = [], []
        for b in blocks:
            if part and len(part) + len(b) > CHUNK:
                out.append(part)
                part = []
            part += b
        if part:
            out.append(part)
        return out

    def run_restore(jid: str, rid: str, rep: list[tuple[str, str, str]], do_files: bool, do_config: bool,
                    save: bool) -> None:
        try:
            z, manifest, text = open_restore(rid)
            with z:
                errors = []
                if do_files and manifest["files"]:
                    total = sum(f["size"] for f in manifest["files"])
                    upd(jid, total=total, done=0)
                    done = 0
                    made = set()
                    for f in manifest["files"]:
                        dest = replace_all(f["path"], [r for r in rep if r[0] == "fs"])
                        fs, _, rest = dest.partition(":")
                        d, n = (f"{fs}:/{rest.lstrip('/').rsplit('/', 1)[0]}", rest.rsplit("/", 1)[1]) \
                            if "/" in rest.lstrip("/") else (f"{fs}:", rest.lstrip("/"))
                        if d not in made:
                            if d.rstrip("/") != f"{fs}:":
                                files.mkdirs(d)
                            made.add(d)
                        try:
                            have = files.list_dir(d).get(n)
                        except Exception:
                            have = None
                        if have == f["size"]:
                            log(jid, f"{dest} ist schon da")
                        else:
                            upd(jid, step=f"Übertrage {dest} ({f['size'] // 1024} KB) …")
                            files.upload(z.read(f["arc"]), dest)
                        done += f["size"]
                        upd(jid, done=done)
                if do_config:
                    cfg = replace_all(text, rep)
                    parts = chunks(cfg)
                    for i, part in enumerate(parts, 1):
                        upd(jid, step=f"Konfiguration {i}/{len(parts)} …")
                        res = apply(part)
                        if not res.get("ok"):
                            errors.append(res.get("error") or "Fehler")
                            log(jid, f"Teil {i}: {res.get('error')}")
                    final = ["voice register global", " create profile"] if "voice register" in cfg else []
                    if re.search(r"^ephone", cfg, re.M):
                        final += ["telephony-service", " create cnf-files"]
                    if final:
                        upd(jid, step="Telefon-Profile erzeugen …")
                        res = apply(final)
                        if not res.get("ok"):
                            errors.append(res.get("error") or "Fehler")
                if save and not errors:
                    upd(jid, step="write memory …")
                    router.save()
            upd(jid, state="done" if not errors else "warn", ts_end=time.time(),
                step="Fertig" + (f" – {len(errors)} Teil(e) mit Fehlern, siehe unten" if errors else ""),
                errors=errors, saved=save and not errors)
        except Exception as e:
            msg = str(e) if isinstance(e, (BackupError, media.MediaError)) else f"{type(e).__name__}: {e}"
            upd(jid, state="error", step="Abgebrochen", error=msg, ts_end=time.time())

    # ---------------- API ----------------

    @app.errorhandler(BackupError)
    def on_backup_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/backup.js")
    def backup_js():
        return send_file(BASE / "static" / "backup.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/backup/job")
    def backup_job():
        with job_lock:
            return jsonify(ok=True, job=dict(job))

    @app.post("/api/backup")
    def backup_start():
        b = request.get_json(force=True) or {}
        jid = start("backup")
        threading.Thread(target=run_backup, args=(jid, bool(b.get("files"))), daemon=True).start()
        with job_lock:
            return jsonify(ok=True, job=dict(job))

    @app.get("/api/backup/download/<jid>")
    def backup_download(jid):
        if not RE_ID.match(jid) or not (work / f"{jid}.zip").exists():
            raise BackupError("Die Sicherung ist nicht mehr da – bitte neu erstellen.")
        with job_lock:
            name = job.get("name") if job.get("id") == jid else None
        return send_file(work / f"{jid}.zip", mimetype="application/zip", as_attachment=True,
                         download_name=name or f"cme-backup-{jid}.zip")

    @app.post("/api/restore/upload")
    def restore_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            raise BackupError("Keine Datei.")
        rid = uuid.uuid4().hex[:12]
        p = work / f"restore-{rid}.zip"
        f.save(p)
        if not zipfile.is_zipfile(p):
            p.unlink(missing_ok=True)
            raise BackupError("Die Datei ist kein ZIP-Archiv.")
        for old in work.glob("restore-*.zip"):
            if old != p and time.time() - old.stat().st_mtime > 6 * 3600:
                old.unlink(missing_ok=True)
        try:
            z, manifest, text = open_restore(rid)
        except BackupError:
            p.unlink(missing_ok=True)
            raise
        with z:
            pass
        heads: dict[str, int] = {}
        for h in (l for l in text.splitlines() if l and not l.startswith(" ")):
            m = re.match(r"^(dial-peer voice|dial-peer cor|voice register (?:dn|pool|template)|ephone-dn|ephone|"
                         r"tftp-server|voice translation-rule|voice translation-profile|voice class \S+|"
                         r"voice hunt-group)", h)
            key = m.group(1) if m else h
            heads[key] = heads.get(key, 0) + 1
        type6 = [l.strip() for l in text.splitlines() if re.search(r"\bpassword 6 ", l)]
        here = (re.search(r"^hostname (\S+)", live("show running-config | include ^hostname"), re.M) or [None, ""])[1]
        return jsonify(ok=True, id=rid, manifest=manifest, lines=len(text.splitlines()),
                       summary=sorted(heads.items(), key=lambda kv: kv[0]), type6=len(type6),
                       replace=suggestions(text, [f["path"] for f in manifest["files"]]),
                       hostname_here=here, preview=text[:20000])

    @app.post("/api/restore/run")
    def restore_run():
        b = request.get_json(force=True) or {}
        rid = str(b.get("id") or "")
        z, manifest, text = open_restore(rid)
        z.close()
        rep = []
        for r in b.get("replace") or []:
            kind, old, new = str(r.get("kind") or ""), str(r.get("old") or "").strip(), str(r.get("new") or "").strip()
            if kind not in ("ip", "fs", "if") or not old:
                continue
            if kind == "ip" and not RE_IP.fullmatch(new):
                raise BackupError(f"Neue Adresse für {old} ist keine IPv4-Adresse.")
            if kind == "fs" and not re.fullmatch(r"[a-z]+\d?:", new):
                raise BackupError(f"Speicherort für {old}: z. B. bootflash: oder flash:")
            if kind == "if" and not re.fullmatch(r"[A-Za-z][A-Za-z0-9/.\-]{1,40}", new):
                raise BackupError(f"Ungültige Schnittstelle für {old}.")
            rep.append((kind, old, new))
        jid = start("restore")
        threading.Thread(target=run_restore, daemon=True,
                         args=(jid, rid, rep, bool(b.get("files")), bool(b.get("config")), bool(b.get("save")))).start()
        with job_lock:
            return jsonify(ok=True, job=dict(job))

    # Dial-Peers, SIP-Tenants und SIP-Profile bearbeiten (dialpeers.py)
    import dialpeers
    dialpeers.register(app, router, apply, demo)
