"""
firmware.py - Firmware fuer die Cisco-Telefone (Tab "Telefone & Rufnummern")

Nimmt das ZIP-Paket von Cisco an (z. B. cmterm-8845_65-sip.14-3-1-0001-136.zip, die Variante
fuer CME/Fremdsysteme, nicht die .cop-Datei fuer CUCM), laedt alle Dateien per SCP nach
<CME_MEDIA_DIR>/firmware/<load>/, setzt fuer jede Datei einen tftp-server-Alias (Dateiname)
und weist die Firmware einem Telefontyp zu:

  voice register global / load <typ> <load>  / create profile
  voice register pool <n> / reset             (optional, alle Telefone dieses Typs)

Hochladen laeuft im Hintergrund (Fortschritt ueber /api/firmware/job), weil die Pakete
100-200 MB gross sind und SCP zum Router einige Minuten braucht.
Wird aus e164.register() eingehaengt; Oberflaeche: static/firmware.js.
"""

from __future__ import annotations

import os
import posixpath
import re
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from flask import jsonify, request, send_file

import media

BASE = Path(__file__).resolve().parent
FW_DIR = f"{media.DEFAULT_BASE}/firmware"
MAX_FW_UPLOAD = 700 * 1024 * 1024
RE_LOAD = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
RE_FILE = re.compile(r"^[A-Za-z0-9_.\-]{1,96}$")
RE_TYPE = re.compile(r"^[A-Za-z0-9_\-]{1,20}$")

# Welche Telefontypen eine Firmware (Name der .loads-Datei) bedient
LOAD_TYPES = [
    (re.compile(r"(?i)^sip8845_65\."), ["8845", "8865", "8865NR"]),
    (re.compile(r"(?i)^sip88xx\."), ["8811", "8841", "8851", "8851NR", "8861"]),
    (re.compile(r"(?i)^sip8832\."), ["8832"]),
    (re.compile(r"(?i)^sip8821\."), ["8821"]),
    (re.compile(r"(?i)^sip78xx\."), ["7811", "7821", "7841", "7861"]),
    (re.compile(r"(?i)^sip7832\."), ["7832"]),
    (re.compile(r"(?i)^sip69"), ["6901", "6911", "6921", "6941", "6945", "6961"]),
    (re.compile(r"(?i)^sip89"), ["8941", "8945"]),
    (re.compile(r"(?i)^sip99"), ["8961", "9951", "9971"]),
]


class FWError(ValueError):
    pass


def types_for(load: str) -> list[str]:
    for rx, types in LOAD_TYPES:
        if rx.search(load):
            return types
    return []


def parse_loads(glob: str) -> dict[str, str]:
    """voice register global -> {typ: load}"""
    out = {}
    for line in glob.splitlines():
        m = re.match(r"^\s+load\s+(\S+)\s+(\S+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def parse_pool_types(pools: str) -> dict[str, list[str]]:
    """voice register pool -> {typ: [pool, ...]}"""
    res, cur = {}, None
    for line in pools.splitlines():
        m = re.match(r"^voice register pool\s+(\d+)", line)
        if m:
            cur = m.group(1)
            continue
        if not line.startswith(" "):
            cur = None
        mt = re.match(r"^\s+type\s+(\S+)", line)
        if cur and mt:
            res.setdefault(mt.group(1), []).append(cur)
    return res


def parse_free(out: str) -> int | None:
    m = re.search(r"\((\d+)\s+bytes free\)", out)
    return int(m.group(1)) if m else None


def parse_phone_load(out: str) -> dict[str, str]:
    """show voice register pool phone-load -> {pool: version} (soweit erkennbar)"""
    res = {}
    if "Invalid" in out or "Demo" in out:
        return res
    for line in out.splitlines():
        m = re.match(r"^\s*(\d+)\s+.*?\b((?:sip|SIP)[\w.\-]+|\d+\.\d+[\w.\-()]*)\s*$", line)
        if m:
            res[m.group(1)] = m.group(2)
    return res


def register(app, router, apply) -> None:
    demo = type(router).__name__ == "DemoRouter"
    files = media.DemoFiles(router) if demo else media.RouterFiles(router)
    app.config["MAX_CONTENT_LENGTH"] = max(app.config.get("MAX_CONTENT_LENGTH") or 0, MAX_FW_UPLOAD)
    job: dict = {}
    job_lock = threading.Lock()

    # ---------------- Router lesen ----------------

    def aliases() -> dict[str, str]:
        return media.parse_tftp_aliases(router.show("show running-config | include ^tftp-server"))

    def flash_free() -> int | None:
        if demo:
            return 3_500_000_000 - sum(len(b) for b in router.files.values())
        fs = FW_DIR.split(":", 1)[0] + ":"
        try:
            return parse_free(router.show(f"dir {fs} | include bytes free"))
        except Exception:
            return None

    def installed(al: dict[str, str]) -> list[dict]:
        """Firmware-Staende anhand der tftp-Aliase auf *.loads"""
        out = []
        for alias, path in sorted(al.items()):
            if not alias.lower().endswith(".loads"):
                continue
            load = alias[:-6]
            d = path.rsplit("/", 1)[0]
            own = d == f"{FW_DIR}/{load}"
            out.append({"load": load, "path": path, "dir": d, "own": own,
                        "file_list": fw_files(al, load, path, own), "types": types_for(load)})
            out[-1]["files"] = len(out[-1]["file_list"])
        return out

    def fw_files(al: dict[str, str], load: str, path: str, own: bool) -> list[dict]:
        """Dateien einer Firmware: eigener Ordner komplett, sonst alle Dateien im Ordner der
        .loads-Datei, deren Name die Versionsnummer traegt (z. B. 14-2-1-0101-1)."""
        prefix = lambda p: re.match(r"^(.*[:/])", p).group(1) if re.match(r"^(.*[:/])", p) else ""
        if own:
            return [{"alias": a, "path": p} for a, p in sorted(al.items()) if p.startswith(prefix(path))]
        version = load.split(".", 1)[1] if "." in load else load
        return [{"alias": a, "path": p} for a, p in sorted(al.items())
                if prefix(p) == prefix(path) and version in p[len(prefix(p)):]]

    def state() -> dict:
        al = aliases()
        glob = router.show("show running-config | section voice register global")
        pools = router.show("show running-config | section voice register pool")
        loads = parse_loads(glob)
        ptypes = parse_pool_types(pools)
        phone = {} if demo else parse_phone_load(router.show("show voice register pool phone-load"))
        fws = installed(al)
        for f in fws:
            f["used_by"] = sorted(t for t, l in loads.items() if l == f["load"])
        types = []
        known = {t for _, ts in LOAD_TYPES for t in ts}
        for t in sorted(set(ptypes) | set(loads)):
            # passende Firmware anbieten; bei unbekanntem Typ alle
            choices = [f["load"] for f in fws if t in f["types"]] if t in known else [f["load"] for f in fws]
            types.append({"type": t, "pools": ptypes.get(t, []), "load": loads.get(t, ""), "choices": choices,
                          "load_ok": not loads.get(t) or any(f["load"] == loads[t] for f in fws),
                          "phone_loads": {p: phone[p] for p in ptypes.get(t, []) if p in phone}})
        return {"firmware": fws, "types": types, "free": flash_free(), "dir": FW_DIR}

    # ---------------- Konfiguration ----------------

    def do_apply(lines: list[str]) -> dict:
        if not demo:
            return apply(lines)
        # Demo: load-Zeilen selbst pflegen (der Demo-Router kennt sie nicht als Einzelwert)
        rest, glob = [], None
        for line in lines:
            m = re.match(r"^\s+(no\s+)?load\s+(\S+)(?:\s+(\S+))?$", line)
            if m and glob:
                i = router.cfg.index("voice register global") + 1
                j = i
                while j < len(router.cfg) and router.cfg[j].startswith(" "):
                    j += 1
                router.cfg[i:j] = [l for l in router.cfg[i:j] if not re.match(rf"^\s+load\s+{re.escape(m.group(2))}\s", l)]
                if not m.group(1):
                    router.cfg.insert(i, f" load {m.group(2)} {m.group(3)}")
                continue
            glob = line.strip() == "voice register global" or (glob and line.startswith(" "))
            rest.append(line)
        return apply(rest) if rest else {"ok": True, "error": None, "config": lines}

    # ---------------- Hochladen (Hintergrund) ----------------

    def run_job(zpath: str, jid: str) -> None:
        def upd(**kw):
            with job_lock:
                if job.get("id") == jid:
                    job.update(kw)
        try:
            with zipfile.ZipFile(zpath) as z:
                members = [i for i in z.infolist() if not i.is_dir()
                           and not i.filename.startswith("__MACOSX/")
                           and not posixpath.basename(i.filename).startswith(".")]
                names = {}
                for i in members:
                    n = posixpath.basename(i.filename)
                    if not RE_FILE.match(n):
                        raise FWError(f"Unerwarteter Dateiname im Paket: {n}")
                    if n in names:
                        raise FWError(f"Datei {n} kommt im Paket doppelt vor.")
                    names[n] = i
                loads = sorted(n[:-6] for n in names if n.lower().endswith(".loads"))
                if not loads:
                    raise FWError("Im Paket ist keine .loads-Datei. Benötigt wird das ZIP für Fremdsysteme/CME "
                                  "(cmterm-…zip), nicht die .cop-Datei für CUCM.")
                total = sum(i.file_size for i in names.values())
                free = flash_free()
                if free is not None and free < total + 20 * 1024 * 1024:
                    raise FWError(f"Zu wenig Platz im Flash: {free // 1048576} MB frei, "
                                  f"{total // 1048576} MB benötigt. Alte Firmware zuerst löschen.")
                # Ordner nach der ersten .loads-Datei benennen
                d = f"{FW_DIR}/{loads[0]}"
                upd(step="Ordner anlegen", total=total, done=0, loads=loads, dir=d)
                files.mkdirs(d)
                have = files.list_dir(d)
                done = 0
                for n, info in sorted(names.items()):
                    if have.get(n) == info.file_size:
                        done += info.file_size
                        upd(step=f"{n} ist schon da", done=done)
                        continue
                    upd(step=f"Lade {n} ({info.file_size // 1024} KB) …")
                    data = z.read(info)
                    files.upload(data, f"{d}/{n}")
                    done += info.file_size
                    upd(done=done)
            upd(step="tftp-server-Einträge setzen")
            al = aliases()
            cfg = []
            for n in sorted(names):
                path = f"{d}/{n}"
                old = al.get(n)
                if old == path:
                    continue
                if old:
                    cfg.append(f"no tftp-server {old} alias {n}")
                cfg.append(f"tftp-server {path} alias {n}")
            res = do_apply(cfg) if cfg else {"ok": True, "error": None}
            if not res.get("ok"):
                raise FWError(f"tftp-server-Einträge: {res.get('error')}")
            upd(state="done", step=f"Fertig: {', '.join(loads)}", ts_end=time.time())
        except Exception as e:
            msg = str(e) if isinstance(e, (FWError, media.MediaError, zipfile.BadZipFile)) \
                else f"{type(e).__name__}: {e}"
            upd(state="error", step="Abgebrochen", error=msg, ts_end=time.time())
        finally:
            try:
                os.unlink(zpath)
            except OSError:
                pass

    # ---------------- API ----------------

    @app.errorhandler(FWError)
    def on_fw_err(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/static/firmware.js")
    def firmware_js():
        return send_file(BASE / "static" / "firmware.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/firmware")
    def fw_list():
        st = state()
        with job_lock:
            j = dict(job)
        return jsonify(ok=True, job=j, **st)

    @app.get("/api/firmware/job")
    def fw_job():
        with job_lock:
            return jsonify(ok=True, job=dict(job))

    @app.post("/api/firmware/upload")
    def fw_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            raise FWError("Keine Datei.")
        if not f.filename.lower().endswith(".zip"):
            raise FWError("Bitte das ZIP-Paket der Firmware hochladen (cmterm-…zip).")
        with job_lock:
            if job.get("state") == "running":
                raise FWError("Es läuft schon ein Hochladen.")
        fd, zpath = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        f.save(zpath)
        if not zipfile.is_zipfile(zpath):
            os.unlink(zpath)
            raise FWError("Die Datei ist kein gültiges ZIP-Archiv.")
        jid = uuid.uuid4().hex[:8]
        with job_lock:
            job.clear()
            job.update(id=jid, state="running", step="Paket prüfen", file=f.filename, total=0, done=0,
                       ts_start=time.time())
        threading.Thread(target=run_job, args=(zpath, jid), daemon=True).start()
        return jsonify(ok=True, job=dict(job))

    @app.post("/api/firmware/assign")
    def fw_assign():
        b = request.get_json(force=True) or {}
        ptype = str(b.get("type") or "").strip()
        load = str(b.get("load") or "").strip()
        if not RE_TYPE.match(ptype):
            raise FWError("Ungültiger Telefontyp.")
        if load and not RE_LOAD.match(load):
            raise FWError("Ungültiger Firmware-Name.")
        st = state()
        if load and not any(f["load"] == load for f in st["firmware"]):
            raise FWError(f"Firmware {load} ist am Router nicht vorhanden.")
        cur = next((t for t in st["types"] if t["type"] == ptype), {"load": "", "pools": []})
        lines = ["voice register global"]
        if load:
            lines.append(f" load {ptype} {load}")
        elif cur["load"]:
            lines.append(f" no load {ptype} {cur['load']}")
        else:
            return jsonify(ok=True, config=[], unchanged=True)
        lines.append(" create profile")
        if b.get("reset"):
            for p in cur["pools"]:
                lines += [f"voice register pool {p}", " reset"]
        res = do_apply(lines)
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.delete("/api/firmware/<load>")
    def fw_delete(load):
        if not RE_LOAD.match(load):
            raise FWError("Ungültiger Firmware-Name.")
        st = state()
        fw = next((f for f in st["firmware"] if f["load"] == load), None)
        if not fw:
            raise FWError(f"Firmware {load} nicht gefunden.")
        if fw["used_by"]:
            raise FWError(f"Firmware {load} ist noch zugewiesen ({', '.join(fw['used_by'])}).")
        d = fw["dir"]
        cfg = [f"no tftp-server {f['path']} alias {f['alias']}" for f in fw["file_list"]]
        res = do_apply(cfg) if cfg else {"ok": True, "error": None, "config": []}
        if res.get("ok"):
            if demo:
                gone = [p for p in router.files if p.startswith(d + "/")] if fw["own"] \
                    else [f["path"] for f in fw["file_list"]]
                for p in gone:
                    router.files.pop(p, None)
            elif fw["own"]:
                router._do(lambda c: c.send_command_timing(f"delete /force /recursive {d}", read_timeout=120))
            else:
                # manuell installiert: nur die Dateien dieser Firmware, der Ordner bleibt
                for f in fw["file_list"]:
                    files.delete(f["path"])
        res.pop("output", None)
        return jsonify(res), (200 if res.get("ok") else 400)

    # Demo: eine installierte Firmware fuer 8851 vorgeben
    if demo and not any(a.endswith(".loads") for a in aliases()):
        d = f"{FW_DIR}/sip88xx.14-4-1-0301-6"
        cfg = []
        for n, size in (("sip88xx.14-4-1-0301-6.loads", 600), ("kern88xx.14-4-1-0301-6.sbn", 4_000_000),
                        ("rootfs88xx.14-4-1-0301-6.sbn", 90_000_000)):
            router.files[f"{d}/{n}"] = b"\0" * min(size, 4096)
            cfg.append(f"tftp-server {d}/{n} alias {n}")
        # dazu eine alte, von Hand in den Flash kopierte Firmware
        for n in ("sip8845_65.14-1-1-0001-125.loads", "kern8845_65.14-1-1-0001-125.sbn",
                  "rootfs8845_65.14-1-1-0001-125.sbn"):
            router.files[f"bootflash:/{n}"] = b"\0" * 1024
            cfg.append(f"tftp-server bootflash:/{n} alias {n}")
        router.tftp.extend(cfg)
