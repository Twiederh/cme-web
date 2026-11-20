"""
media.py - Hintergrundbilder und Klingeltoene fuer die Telefone

Rechnet hochgeladene Bilder und Audiodateien in die Formate der Cisco-Telefone um,
laedt sie per SCP in den Flash des Routers, setzt die tftp-server-Aliase und pflegt
die Listen (Desktops/800x480x24/List.xml, RingList.xml, Ringlist-wb.xml).

Formate:
- Hintergrund 88xx: PNG 800x480, 8 Bit RGB; Vorschaubild 139x109
- Klingelton: Rohdaten 8 kHz, 8 Bit mu-law, mono, Laenge ein Vielfaches von
  240 Byte, hoechstens 16080 Byte (gut 2 Sekunden)

Lokaler Cache: Vorschaubilder und Klingeltoene liest die Seite nur einmal vom Router
(per "more /binary", langsam) und legt sie unter CME_MEDIA_CACHE ab
(Standard: <VM_CONFIG_DIR>/media-cache, also im Volume vm-config). Beim Hochladen
kommen die neuen Dateien direkt in den Cache, beim Loeschen fliegen sie raus.
Alle 30 Minuten (und nach jeder Aenderung) vergleicht die Seite die Dateigroessen mit dem
Router und verwirft Abweichungen.
"""

from __future__ import annotations

import io
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from flask import Response, jsonify, request

WALL_RES = "800x480x24"
WALL_SIZE = (800, 480)
THUMB_SIZE = (139, 109)
RING_MAX = 16080
RING_STEP = 240
RING_RATE = 8000
RING_LISTS = ("RingList.xml", "Ringlist-wb.xml", "DistinctiveRingList.xml")
DEFAULT_BASE = os.environ.get("CME_MEDIA_DIR", "bootflash:/its")
MAX_UPLOAD = 30 * 1024 * 1024

RE_SLUG = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
RE_DISPLAY = re.compile(r"^[A-Za-z0-9 äöüÄÖÜß_.,()/+\-]{1,40}$")
RE_FILE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
SCP_RETRIES = 4


class MediaError(ValueError):
    pass


# --------------------------------------------------------------------------
# Umrechnen
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    repl = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
    s = "".join(repl.get(c, c) for c in (text or "").strip())
    s = re.sub(r"[^A-Za-z0-9_\-]+", "_", s).strip("_")
    return s[:40]


def convert_image(data: bytes) -> tuple[bytes, bytes]:
    """Beliebiges Bild -> (PNG 800x480, Vorschaubild 139x109), mittig zugeschnitten."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise MediaError("Pillow fehlt (pip install pillow).")
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise MediaError("Die Datei ist kein lesbares Bild.")
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[3])
        im = bg
    else:
        im = im.convert("RGB")
    full = ImageOps.fit(im, WALL_SIZE, Image.LANCZOS)
    thumb = ImageOps.fit(full, THUMB_SIZE, Image.LANCZOS)

    def png(img) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)   # ohne Metadaten, 8 Bit RGB
        return buf.getvalue()

    return png(full), png(thumb)


def _ulaw_encode(sample: int) -> int:
    bias, clip = 0x84, 32635
    sign = 0
    if sample < 0:
        sample, sign = -sample, 0x80
    sample = min(sample, clip) + bias
    exponent = max((sample >> 7).bit_length() - 1, 0)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_decode(byte: int) -> int:
    u = ~byte & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    s = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -s if sign else s


ULAW_DECODE = [_ulaw_decode(i) for i in range(256)]


def ulaw_to_wav(raw: bytes) -> bytes:
    import array
    pcm = array.array("h", (ULAW_DECODE[b] for b in raw))
    if pcm.itemsize != 2:
        raise MediaError("Unerwartete Plattform")
    import sys
    if sys.byteorder == "big":
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RING_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def convert_ringtone(data: bytes, filename: str = "", start: float = 0.0) -> bytes:
    """Beliebige Audio-/Videodatei -> Cisco-Klingelton (8 kHz mu-law, max. 16080 Byte)."""
    import array
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError("ffmpeg fehlt auf dem Server.")
    suffix = os.path.splitext(filename or "")[1][:8] or ".bin"
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in" + suffix)
        with open(src, "wb") as f:
            f.write(data)
        cmd = [ffmpeg, "-v", "error", "-y"]
        if start > 0:
            cmd += ["-ss", f"{start:.2f}"]
        cmd += ["-i", src, "-vn", "-ac", "1", "-ar", str(RING_RATE), "-f", "s16le", "-"]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise MediaError("Umrechnen hat zu lange gedauert.")
    if res.returncode != 0 or not res.stdout:
        msg = res.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unbekannter Fehler"]
        raise MediaError(f"Die Datei enthaelt keine lesbare Tonspur ({msg[0]}).")
    pcm = array.array("h")
    pcm.frombytes(res.stdout[: len(res.stdout) // 2 * 2])
    if __import__("sys").byteorder == "big":
        pcm.byteswap()
    # Stille am Anfang abschneiden
    threshold = 300
    first = next((i for i, s in enumerate(pcm) if abs(s) > threshold), None)
    if first is None:
        raise MediaError("Die Tonspur ist stumm.")
    pcm = pcm[max(first - 40, 0):]
    pcm = pcm[:RING_MAX]
    n = max(RING_STEP, -(-len(pcm) // RING_STEP) * RING_STEP)
    pcm.extend([0] * (n - len(pcm)))
    # Lautstaerke anheben (Spitze etwa -1 dB) und weich ein-/ausblenden
    peak = max(abs(s) for s in pcm) or 1
    gain = min(29000 / peak, 8.0)
    fade_in, fade_out = 40, 240
    out = bytearray(n)
    for i, s in enumerate(pcm):
        f = 1.0
        if i < fade_in:
            f = i / fade_in
        elif i >= n - fade_out:
            f = (n - i) / fade_out
        out[i] = _ulaw_encode(int(max(-32768, min(32767, s * gain * f))))
    return bytes(out)


# --------------------------------------------------------------------------
# Listen lesen und schreiben
# --------------------------------------------------------------------------

def parse_wall_list(xml: str) -> list[dict]:
    items = []
    for m in re.finditer(r"<ImageItem\b([^>]*)/?>", xml or "", re.I):
        attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1)))
        image, url = attrs.get("Image", ""), attrs.get("URL", "")
        if not url:
            continue
        items.append({"image": image, "url": url,
                      "file": posixpath.basename(url.split(":", 1)[-1]),
                      "thumb": posixpath.basename(image.split(":", 1)[-1])})
    return items


def build_wall_list(items: list[dict]) -> str:
    lines = ["<CiscoIPPhoneImageList>"]
    for it in items:
        lines.append(f"<ImageItem Image={quoteattr(it['image'])} URL={quoteattr(it['url'])}/>")
    lines.append("</CiscoIPPhoneImageList>")
    return "\r\n".join(lines) + "\r\n"


def parse_ring_list(xml: str) -> list[dict]:
    items = []
    for m in re.finditer(r"<Ring>(.*?)</Ring>", xml or "", re.S | re.I):
        name = re.search(r"<DisplayName>(.*?)</DisplayName>", m.group(1), re.S | re.I)
        file = re.search(r"<FileName>(.*?)</FileName>", m.group(1), re.S | re.I)
        if file:
            items.append({"display": (name.group(1).strip() if name else file.group(1).strip()),
                          "file": file.group(1).strip()})
    return items


def build_ring_list(items: list[dict]) -> str:
    lines = ["<CiscoIPPhoneRingList>"]
    for it in items:
        lines += ["<Ring>", f"<DisplayName>{escape(it['display'])}</DisplayName>",
                  f"<FileName>{escape(it['file'])}</FileName>", "</Ring>"]
    lines.append("</CiscoIPPhoneRingList>")
    return "\r\n".join(lines) + "\r\n"


def parse_tftp_aliases(out: str) -> dict[str, str]:
    """show running-config | include ^tftp-server -> {alias: pfad}"""
    result = {}
    for line in (out or "").splitlines():
        m = re.match(r"^tftp-server\s+(\S+)(?:\s+alias\s+(\S+))?", line.strip())
        if m:
            path = m.group(1)
            result[m.group(2) or posixpath.basename(path.split(":", 1)[-1])] = path
    return result


def parse_hexdump(out: str) -> bytes:
    """Ausgabe von 'more /binary' (Offset: Hexgruppen  ASCII) -> Bytes."""
    data = bytearray()
    expect = 0
    for line in (out or "").splitlines():
        m = re.match(r"^\s*([0-9A-Fa-f]{6,8}):\s+(.*)$", line)
        if not m:
            continue
        if int(m.group(1), 16) != expect:
            raise MediaError("Hexdump unvollstaendig")
        hexpart = re.split(r"\s{2,}", m.group(2).strip(), maxsplit=1)[0]
        chunk = bytearray()
        for group in hexpart.split():
            if not re.fullmatch(r"(?:[0-9A-Fa-f]{2})+", group):
                break
            chunk += bytes.fromhex(group)
        data += chunk
        expect += len(chunk)
    if not data:
        raise MediaError("Keine Daten")
    return bytes(data)


# --------------------------------------------------------------------------
# Dateizugriff auf dem Router
# --------------------------------------------------------------------------

DIR_LINE_RE = re.compile(r"^\s*\d+\s+[-drwx]+\s+(\d+)\s+.*?\s(\S+)\s*$")


class RouterFiles:
    """Dateioperationen ueber die netmiko-Verbindung der Klasse Router aus cme_web."""

    def __init__(self, router):
        self.r = router

    def read_text(self, path: str) -> str | None:
        out = self.r._do(lambda c: c.send_command(f"more {path}", read_timeout=60))
        if "%Error" in out or "No such file" in out:
            return None
        return out

    def read_binary(self, path: str) -> bytes:
        out = self.r._do(lambda c: c.send_command(f"more /binary {path}", read_timeout=180))
        return parse_hexdump(out)

    def list_dir(self, d: str) -> dict[str, int]:
        out = self.r._do(lambda c: c.send_command(f"dir {d}", read_timeout=60))
        res = {}
        for line in out.splitlines():
            m = DIR_LINE_RE.match(line)
            if m:
                res[m.group(2)] = int(m.group(1))
        return res

    def mkdirs(self, d: str) -> None:
        fs, _, rest = d.partition(":")
        cur = f"{fs}:"

        def run(c):
            nonlocal cur
            for part in [p for p in rest.split("/") if p]:
                cur = f"{cur}/{part}"
                out = c.send_command_timing(f"mkdir {cur}")
                if "?" in out or "[" in out:
                    c.send_command_timing("\n")
        self.r._do(run)

    def delete(self, path: str) -> None:
        self.r._do(lambda c: c.send_command_timing(f"delete /force {path}"))

    def upload(self, data: bytes, dest: str) -> None:
        from netmiko import SCPConn
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
            local = tmp.name
        try:
            for attempt in range(1, SCP_RETRIES + 1):
                try:
                    def put(c):
                        scp = SCPConn(c, socket_timeout=300.0)
                        try:
                            scp.scp_transfer_file(local, dest)
                        finally:
                            try:
                                scp.close()
                            except Exception:
                                pass
                    self.r._do(put)
                    break
                except Exception as e:
                    if attempt == SCP_RETRIES:
                        raise MediaError(f"Upload nach {dest} fehlgeschlagen ({type(e).__name__}: {e}). "
                                         "Ist 'ip scp server enable' gesetzt?")
                    time.sleep(2 * attempt)
                    # IOS beendet die SSH-Sitzung nach SCP gern: neu verbinden lassen
                    try:
                        if self.r.conn:
                            self.r.conn.disconnect()
                    except Exception:
                        pass
                    self.r.conn = None
        finally:
            os.unlink(local)
        d, name = dest.rsplit("/", 1)
        size = self.list_dir(d).get(name)
        if size != len(data):
            raise MediaError(f"Nach dem Upload hat {dest} {size} statt {len(data)} Byte.")


class DemoFiles:
    """Dateien im Speicher fuer den Demo-Modus."""

    def __init__(self, router):
        self.r = router

    def read_text(self, path):
        b = self.r.files.get(path)
        return None if b is None else b.decode("utf-8", "replace")

    def read_binary(self, path):
        if path not in self.r.files:
            raise MediaError("nicht gefunden")
        return self.r.files[path]

    def list_dir(self, d):
        d = d.rstrip("/") + "/"
        return {p[len(d):]: len(b) for p, b in self.r.files.items()
                if p.startswith(d) and "/" not in p[len(d):]}

    def mkdirs(self, d):
        pass

    def delete(self, path):
        self.r.files.pop(path, None)

    def upload(self, data, dest):
        time.sleep(0.3)
        self.r.files[dest] = bytes(data)


def demo_seed(router) -> None:
    """Beispielbilder und -toene fuer den Demo-Modus."""
    import math
    router.files = {}
    base = DEFAULT_BASE
    desk = f"{base}/Desktops/{WALL_RES}"
    ring = f"{base}/Ringtones"
    walls = []
    try:
        from PIL import Image
        for name, color in (("Blau", (40, 90, 170)), ("Gruen", (40, 140, 90))):
            img = Image.new("RGB", WALL_SIZE, color)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            full, thumb = convert_image(buf.getvalue())
            router.files[f"{desk}/{name}.png"] = full
            router.files[f"{desk}/{name}_thumb.png"] = thumb
            walls.append({"image": f"TFTP:Desktops/{WALL_RES}/{name}_thumb.png",
                          "url": f"TFTP:Desktops/{WALL_RES}/{name}.png"})
    except ImportError:
        pass
    router.files[f"{desk}/List.xml"] = build_wall_list(walls).encode()
    rings = []
    for name, freq in (("Ring1", 440), ("Ring2", 660)):
        raw = bytes(_ulaw_encode(int(12000 * math.sin(2 * math.pi * freq * i / RING_RATE)
                                     * (1 if (i // 1600) % 2 == 0 else 0)))
                    for i in range(8000))
        router.files[f"{ring}/{name}.raw"] = raw
        rings.append({"display": name, "file": f"{name}.raw"})
    for lst in ("RingList.xml", "Ringlist-wb.xml"):
        router.files[f"{ring}/{lst}"] = build_ring_list(rings).encode()
    # Bilder liegen unter dem Alias Desktops/..., Klingeltoene unter dem Dateinamen
    router.tftp = []
    for p in router.files:
        rel = p[len(base) + 1:]
        alias = rel if rel.startswith("Desktops/") else posixpath.basename(rel)
        router.tftp.append(f"tftp-server {p} alias {alias}")


# --------------------------------------------------------------------------
# Web-Endpunkte
# --------------------------------------------------------------------------

class MediaCache:
    """Dateien vom Router: im Speicher und auf der Platte (ueberlebt Neustarts des Containers)."""

    def __init__(self, directory: Path | None):
        self.mem: dict[str, bytes] = {}
        self.dir = None
        if directory:
            try:
                directory.mkdir(parents=True, exist_ok=True)
                probe = directory / ".probe"
                probe.write_bytes(b"")
                probe.unlink()
                self.dir = directory
            except OSError:
                self.dir = None          # nicht beschreibbar: nur im Speicher

    @staticmethod
    def _name(path: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.\-]", "_", path)[:200]

    def get(self, path: str) -> bytes | None:
        if path in self.mem:
            return self.mem[path]
        if self.dir:
            try:
                data = (self.dir / self._name(path)).read_bytes()
            except OSError:
                return None
            self.mem[path] = data
            return data
        return None

    def has(self, path: str) -> bool:
        return path in self.mem or bool(self.dir and (self.dir / self._name(path)).exists())

    def size(self, path: str) -> int | None:
        if path in self.mem:
            return len(self.mem[path])
        if self.dir:
            try:
                return (self.dir / self._name(path)).stat().st_size
            except OSError:
                return None
        return None

    def put(self, path: str, data: bytes) -> None:
        self.mem[path] = data
        if self.dir:
            f = self.dir / self._name(path)
            tmp = f.with_name(f".{f.name}.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(f)
            except OSError:
                pass

    def drop(self, path: str | None) -> None:
        if not path:
            return
        self.mem.pop(path, None)
        if self.dir:
            (self.dir / self._name(path)).unlink(missing_ok=True)


def cache_dir(demo: bool) -> Path:
    if os.environ.get("CME_MEDIA_CACHE"):
        return Path(os.environ["CME_MEDIA_CACHE"])
    if demo:
        return Path(tempfile.gettempdir()) / "cme-web-demo-media-cache"
    return Path(os.environ.get("VM_CONFIG_DIR", "/data/asterisk")) / "media-cache"


def register(app, router, apply, demo: bool) -> None:
    files = DemoFiles(router) if demo else RouterFiles(router)
    lock = threading.Lock()
    cache = MediaCache(cache_dir(demo))
    st_cache = {"ts": 0.0, "data": None}
    warm = {"running": False, "checked": 0.0}
    app.config.setdefault("MAX_CONTENT_LENGTH", MAX_UPLOAD)

    def aliases() -> dict[str, str]:
        return parse_tftp_aliases(router.show("show running-config | include ^tftp-server"))

    def locate(al: dict[str, str]) -> dict:
        wall_list = al.get(f"Desktops/{WALL_RES}/List.xml") or f"{DEFAULT_BASE}/Desktops/{WALL_RES}/List.xml"
        ring_list = al.get("RingList.xml") or al.get("Ringlist-wb.xml") or f"{DEFAULT_BASE}/Ringtones/RingList.xml"
        ring_dir = ring_list.rsplit("/", 1)[0]
        lists = {name: al.get(name) for name in RING_LISTS}
        # RingList.xml und Ringlist-wb.xml immer pflegen, DistinctiveRingList.xml nur, wenn vorhanden
        for name in RING_LISTS[:2]:
            lists[name] = lists[name] or f"{ring_dir}/{name}"
        lists = {k: v for k, v in lists.items() if v}
        return {"wall_list": wall_list, "wall_dir": wall_list.rsplit("/", 1)[0],
                "ring_dir": ring_dir, "ring_lists": lists}

    def state(force: bool = False) -> dict:
        # Kurz zwischenspeichern: die Vorschaubilder fragen sonst jedes Mal den Router
        if not force and st_cache["data"] and time.time() - st_cache["ts"] < 20:
            return st_cache["data"]
        al = aliases()
        loc = locate(al)
        walls = parse_wall_list(files.read_text(loc["wall_list"]) or "")
        for w in walls:
            w["name"] = os.path.splitext(w["file"])[0]
            w["alias_ok"] = f"Desktops/{WALL_RES}/{w['file']}" in al
        rings_xml = files.read_text(loc["ring_lists"]["RingList.xml"]) or ""
        rings = parse_ring_list(rings_xml)
        for r in rings:
            r["alias_ok"] = r["file"] in al
        for w in walls:
            w["thumb_path"] = al.get(f"Desktops/{WALL_RES}/{w['thumb']}") or f"{loc['wall_dir']}/{w['thumb']}"
        for r in rings:
            r["path"] = al.get(r["file"]) or f"{loc['ring_dir']}/{r['file']}"
        if force or time.time() - warm["checked"] > 1800:
            warm["checked"] = time.time()
            validate(loc, walls, rings)
        st_cache.update(ts=time.time(), data={"al": al, "loc": loc, "walls": walls, "rings": rings})
        prefetch([w["thumb_path"] for w in walls] + [r["path"] for r in rings])
        return st_cache["data"]

    def validate(loc: dict, walls: list[dict], rings: list[dict]) -> None:
        """Zwischengespeicherte Dateien verwerfen, deren Groesse am Router anders ist."""
        sizes: dict[str, int] = {}
        for d in {loc["wall_dir"], loc["ring_dir"], *(posixpath.dirname(w["thumb_path"]) for w in walls),
                  *(posixpath.dirname(r["path"]) for r in rings)}:
            try:
                sizes.update({f"{d}/{n}": s for n, s in files.list_dir(d).items()})
            except Exception:
                return                   # Router nicht erreichbar: Cache behalten
        for p in [w["thumb_path"] for w in walls] + [r["path"] for r in rings]:
            have = cache.size(p)
            if have is not None and sizes.get(p) != have:
                cache.drop(p)

    def fetch(path: str) -> bytes:
        data = cache.get(path)
        if data is None:
            data = files.read_binary(path)
            cache.put(path, data)
        return data

    def prefetch(paths: list[str]) -> None:
        """Fehlende Vorschaubilder und Klingeltoene im Hintergrund holen."""
        todo = [p for p in paths if not cache.has(p)]
        if not todo or warm["running"]:
            return
        warm["running"] = True

        def run():
            try:
                for p in todo:
                    try:
                        fetch(p)
                    except Exception:
                        pass
            finally:
                warm["running"] = False
        threading.Thread(target=run, daemon=True, name="media-cache").start()

    def write_text(path: str, text: str, al: dict, alias: str) -> list[str]:
        files.delete(path)
        files.upload(text.encode("utf-8"), path)
        cache.drop(path)
        return [] if al.get(alias) == path else [f"tftp-server {path} alias {alias}"]

    def finish(cfg: list[str], **extra):
        st_cache["ts"] = 0
        if cfg:
            res = apply(cfg)
            res.pop("output", None)
        else:
            res = {"ok": True, "error": None, "config": []}
        res.update(extra)
        return jsonify(res), (200 if res["ok"] else 400)

    @app.errorhandler(MediaError)
    def on_media_error(e):
        return jsonify(ok=False, error=str(e)), 400

    @app.get("/api/media")
    def media_list():
        st = state(force=request.args.get("force") == "1")
        return jsonify(ok=True, wallpapers=st["walls"], ringtones=st["rings"],
                       paths={"wallpapers": st["loc"]["wall_dir"], "ringtones": st["loc"]["ring_dir"]},
                       ffmpeg=bool(shutil.which("ffmpeg")) or demo)

    def find_path(al: dict, alias: str, fallback: str) -> str:
        return al.get(alias) or fallback

    @app.get("/api/media/thumb/<name>")
    def media_thumb(name):
        if not RE_FILE.match(name):
            raise MediaError("Ungueltiger Dateiname")
        st = state()
        if not any(w["thumb"] == name for w in st["walls"]):
            return Response(status=404)
        path = find_path(st["al"], f"Desktops/{WALL_RES}/{name}", f"{st['loc']['wall_dir']}/{name}")
        try:
            data = fetch(path)
        except MediaError:
            return Response(status=404)
        return Response(data, mimetype="image/png",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/api/media/ring/<name>")
    def media_ring(name):
        if not RE_FILE.match(name):
            raise MediaError("Ungueltiger Dateiname")
        st = state()
        if not any(r["file"] == name for r in st["rings"]):
            return Response(status=404)
        path = find_path(st["al"], name, f"{st['loc']['ring_dir']}/{name}")
        try:
            data = fetch(path)
        except MediaError:
            return Response(status=404)
        return Response(ulaw_to_wav(data), mimetype="audio/wav",
                        headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/media/wallpapers")
    def media_add_wallpaper():
        up = request.files.get("file")
        if not up:
            raise MediaError("Keine Datei hochgeladen.")
        slug = slugify(request.form.get("name") or os.path.splitext(up.filename or "")[0])
        if not RE_SLUG.match(slug):
            raise MediaError("Bitte einen Namen aus Buchstaben und Ziffern angeben.")
        full, thumb = convert_image(up.read())
        with lock:
            st = state(force=True)
            if any(w["name"].lower() == slug.lower() for w in st["walls"]):
                raise MediaError(f"Ein Hintergrundbild „{slug}“ gibt es schon.")
            d = st["loc"]["wall_dir"]
            files.mkdirs(d)
            cfg = []
            for fname, data in ((f"{slug}.png", full), (f"{slug}_thumb.png", thumb)):
                files.upload(data, f"{d}/{fname}")
                if fname.endswith("_thumb.png"):
                    cache.put(f"{d}/{fname}", data)      # Vorschau gleich im Cache
                cfg.append(f"tftp-server {d}/{fname} alias Desktops/{WALL_RES}/{fname}")
            walls = [{"image": w["image"], "url": w["url"]} for w in st["walls"]]
            walls.append({"image": f"TFTP:Desktops/{WALL_RES}/{slug}_thumb.png",
                          "url": f"TFTP:Desktops/{WALL_RES}/{slug}.png"})
            cfg += write_text(st["loc"]["wall_list"], build_wall_list(walls), st["al"],
                              f"Desktops/{WALL_RES}/List.xml")
        return finish(cfg, name=slug, size=len(full))

    @app.delete("/api/media/wallpapers/<name>")
    def media_del_wallpaper(name):
        if not RE_FILE.match(name):
            raise MediaError("Ungueltiger Name")
        with lock:
            st = state(force=True)
            w = next((w for w in st["walls"] if w["name"] == name), None)
            if not w:
                raise MediaError(f"Hintergrundbild {name} gibt es nicht.")
            rest = [{"image": x["image"], "url": x["url"]} for x in st["walls"] if x is not w]
            cfg = write_text(st["loc"]["wall_list"], build_wall_list(rest), st["al"],
                             f"Desktops/{WALL_RES}/List.xml")
            for fname in (w["file"], w["thumb"]):
                alias = f"Desktops/{WALL_RES}/{fname}"
                path = st["al"].get(alias)
                if path:
                    cfg.append(f"no tftp-server {path} alias {alias}")
                files.delete(path or f"{st['loc']['wall_dir']}/{fname}")
                cache.drop(path or f"{st['loc']['wall_dir']}/{fname}")
        return finish(cfg, name=name)

    @app.post("/api/media/ringtones")
    def media_add_ringtone():
        up = request.files.get("file")
        if not up:
            raise MediaError("Keine Datei hochgeladen.")
        display = (request.form.get("name") or os.path.splitext(up.filename or "")[0]).strip()[:40]
        if not RE_DISPLAY.match(display):
            raise MediaError("Bitte einen Namen aus Buchstaben, Ziffern und Leerzeichen angeben.")
        try:
            start = max(0.0, min(float(request.form.get("start") or 0), 600.0))
        except ValueError:
            raise MediaError("Startzeit muss eine Zahl sein.")
        slug = slugify(display)
        fname = f"{slug}.raw"
        raw = convert_ringtone(up.read(), up.filename or "", start)
        with lock:
            st = state(force=True)
            if any(r["file"].lower() == fname.lower() for r in st["rings"]):
                raise MediaError(f"Einen Klingelton {fname} gibt es schon.")
            d = st["loc"]["ring_dir"]
            files.mkdirs(d)
            files.upload(raw, f"{d}/{fname}")
            cache.put(f"{d}/{fname}", raw)
            cfg = [f"tftp-server {d}/{fname} alias {fname}"]
            for lst, path in st["loc"]["ring_lists"].items():
                items = parse_ring_list(files.read_text(path) or "")
                items.append({"display": display, "file": fname})
                cfg += write_text(path, build_ring_list(items), st["al"], lst)
        return finish(cfg, file=fname, size=len(raw), seconds=round(len(raw) / RING_RATE, 2))

    @app.delete("/api/media/ringtones/<name>")
    def media_del_ringtone(name):
        if not RE_FILE.match(name):
            raise MediaError("Ungueltiger Name")
        with lock:
            st = state(force=True)
            if not any(r["file"] == name for r in st["rings"]):
                raise MediaError(f"Klingelton {name} gibt es nicht.")
            cfg = []
            for lst, path in st["loc"]["ring_lists"].items():
                text = files.read_text(path)
                if text is None:
                    continue
                items = [r for r in parse_ring_list(text) if r["file"] != name]
                cfg += write_text(path, build_ring_list(items), st["al"], lst)
            path = st["al"].get(name)
            if path:
                cfg.append(f"no tftp-server {path} alias {name}")
            files.delete(path or f"{st['loc']['ring_dir']}/{name}")
            cache.drop(path or f"{st['loc']['ring_dir']}/{name}")
        return finish(cfg, file=name)
