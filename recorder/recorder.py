#!/usr/bin/env python3
"""
recorder.py - Aufzeichnungsserver (SIPREC) fuer CUBE / Cisco CME

Der Router kopiert die Sprache externer Gespraeche (media class ... recorder profile ... siprec)
und schickt sie per SIP an diesen Dienst. Je Gespraech kommen zwei Sprachstroeme (eine Richtung
je Strom) und XML-Metadaten mit den Teilnehmern.

Ergebnis je Gespraech in REC_DIR:
  <id>.mp3    Stereo: links die interne Seite, rechts die Gegenseite
  <id>.json   Zeit, Dauer, Richtung, Rufnummern, Abschrift, Mailstatus

Danach optional: Abschrift (Whisper, je Kanal getrennt -> Dialog), Mail mit Anhang,
automatisches Loeschen nach N Tagen. Einstellungen liest der Dienst bei jedem Gespraech aus
REC_CONFIG (schreibt cme-web).

Umgebung: HOST_IP (Adresse fuer SDP), CME_IP (nur von dort Anrufe annehmen), SIP_PORT (5070),
RTP_START/RTP_END (10100-10139), REC_DIR, REC_CONFIG, WHISPER_URL, SMTP_*.
"""

from __future__ import annotations

import email
import json
import os
import random
import re
import shutil
import smtplib
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path
from queue import Queue

VERSION = "1.0"
HOST_IP = os.environ.get("HOST_IP", "")
CME_IPS = {x.strip() for x in os.environ.get("CME_IP", "").split(",") if x.strip()}
SIP_PORT = int(os.environ.get("SIP_PORT", "5070"))
RTP_START = int(os.environ.get("RTP_START", "10100"))
RTP_END = int(os.environ.get("RTP_END", "10139"))
REC_DIR = Path(os.environ.get("REC_DIR", "/recordings"))
REC_CONFIG = Path(os.environ.get("REC_CONFIG", "/config/cme-recording.json"))
IDLE_SECS = int(os.environ.get("REC_IDLE_SECS", "1800"))      # ohne RTP: Gespraech beenden
MAX_SECS = int(os.environ.get("REC_MAX_SECS", str(6 * 3600)))
MAIL_MAX = 18 * 1024 * 1024                                     # groessere Anhaenge weglassen

# RTP-Payload -> Codec (ffmpeg-Format, Bytes -> Samples bei 8 bzw. 16 kHz)
CODECS = {"pcma": "alaw", "pcmu": "mulaw", "g722": "g722"}
STATIC_PT = {0: "pcmu", 8: "pcma", 9: "g722", 13: "cn", 18: "g729"}
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
           "c": "content-type", "k": "supported", "s": "subject"}
ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS, UPDATE, INFO"

LOG = deque(maxlen=60)


def log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    LOG.append(line)
    print(line, flush=True)


def settings() -> dict:
    s = {"enabled": True, "transcript": True, "mail": False, "emails": {}, "default_email": "",
         "retention_days": 90, "min_secs": 3, "internal": [], "own": [], "prefix": "8199"}
    try:
        s.update(json.loads(REC_CONFIG.read_text()))
    except (FileNotFoundError, ValueError):
        pass
    return s


# --------------------------------------------------------------------------
# SIP
# --------------------------------------------------------------------------

class Msg:
    def __init__(self, data: bytes, src: tuple, reply):
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        self.start = lines[0]
        self.headers: list[list[str]] = []
        for line in lines[1:]:
            if line[:1] in (" ", "\t") and self.headers:
                self.headers[-1][1] += " " + line.strip()
                continue
            k, _, v = line.partition(":")
            k = k.strip().lower()
            self.headers.append([COMPACT.get(k, k), v.strip()])
        try:
            n = int(self.get("content-length") or len(body))
        except ValueError:
            n = len(body)
        self.body = body[:n]
        self.src, self.reply = src, reply
        parts = self.start.split()
        self.is_request = not self.start.startswith("SIP/2.0")
        self.method = parts[0].upper() if self.is_request and parts else ""
        self.uri = parts[1] if self.is_request and len(parts) > 1 else ""

    def get(self, name: str) -> str:
        return next((v for k, v in self.headers if k == name), "")

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name]

    @property
    def call_id(self) -> str:
        return self.get("call-id")

    @property
    def cseq(self) -> tuple[int, str]:
        p = self.get("cseq").split()
        return (int(p[0]) if p and p[0].isdigit() else 0, p[1].upper() if len(p) > 1 else "")


def user_of(value: str) -> str:
    m = re.search(r"(?:sips?|tel):\+?([^@;>\s]+)", value or "")
    if not m:
        return ""
    user = m.group(1)
    return ("+" if re.search(r"(?:sips?|tel):\+", value) else "") + user


def respond(req: Msg, code: int, reason: str, to_tag: str = "", body: bytes = b"",
            ctype: str = "", extra: list[str] | None = None) -> bytes:
    out = [f"SIP/2.0 {code} {reason}"]
    out += [f"Via: {v}" for v in req.all("via")]
    to = req.get("to")
    if to_tag and ";tag=" not in to:
        to += f";tag={to_tag}"
    out += [f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.call_id}", f"CSeq: {req.get('cseq')}"]
    if code < 300 and req.method in ("INVITE", "UPDATE"):
        out.append(f"Contact: <sip:recorder@{HOST_IP}:{SIP_PORT}>")
    out += extra or []
    out += [f"Allow: {ALLOW}", "Server: cme-recorder/" + VERSION]
    if body:
        out.append(f"Content-Type: {ctype}")
    out.append(f"Content-Length: {len(body)}")
    data = ("\r\n".join(out) + "\r\n\r\n").encode() + body
    req.reply(data)
    return data


# --------------------------------------------------------------------------
# SDP und Metadaten
# --------------------------------------------------------------------------

def split_body(msg: Msg) -> tuple[str, str]:
    """-> (sdp, metadata-xml) aus einem einfachen oder multipart-Body."""
    ctype = msg.get("content-type").lower()
    if not msg.body:
        return "", ""
    if ctype.startswith("application/sdp"):
        return msg.body.decode("utf-8", "replace"), ""
    if ctype.startswith("multipart/"):
        raw = f"Content-Type: {msg.get('content-type')}\r\n\r\n".encode() + msg.body
        m = email.message_from_bytes(raw)
        sdp = xml = ""
        for part in m.walk():
            t = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            if t == "application/sdp":
                sdp = payload.decode("utf-8", "replace")
            elif "rs-metadata" in t or t.endswith("xml"):
                xml = payload.decode("utf-8", "replace")
        return sdp, xml
    return "", ""


def parse_sdp(text: str) -> list[dict]:
    media = []
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("m="):
            p = line[2:].split()
            media.append({"type": p[0], "port": int(p[1]) if len(p) > 1 and p[1].isdigit() else 0,
                          "proto": p[2] if len(p) > 2 else "RTP/AVP", "fmts": p[3:], "rtpmap": {}, "label": ""})
        elif line.startswith("a=") and media:
            a = line[2:]
            if a.startswith("rtpmap:"):
                pt, _, enc = a[7:].partition(" ")
                if pt.isdigit():
                    media[-1]["rtpmap"][int(pt)] = enc.split("/")[0].lower()
            elif a.startswith("label:"):
                media[-1]["label"] = a[6:].strip()
    return media


def parse_metadata(text: str) -> dict:
    """RFC 7865 (Cisco): Teilnehmer, Stroeme und wer welchen Strom sendet."""
    res = {"participants": [], "streams": {}, "sends": {}}
    if not text.strip():
        return res
    try:
        clean = re.sub(r"""\sxmlns(:[\w.-]+)?=(["']).*?\2""", "", text)
        clean = re.sub(r"<(/?)[\w.-]+:", r"<\1", clean)
        clean = re.sub(r"\s[\w.-]+:([\w.-]+=)", r" \1", clean)
        clean = re.sub(r"^<\?xml[^>]*\?>", "", clean.strip())
        root = ET.fromstring(clean)
    except ET.ParseError as e:
        log(f"Metadaten nicht lesbar: {e}")
        return res
    for p in root.iter("participant"):
        pid = p.get("participant_id") or p.get("id") or ""
        nid = p.find("nameID") if p.find("nameID") is not None else p.find("nameid")
        aor = (nid.get("aor") if nid is not None else "") or p.findtext("aor") or ""
        name = ((nid.findtext("name") if nid is not None else "") or "").strip()
        res["participants"].append({"id": pid, "number": user_of(aor), "name": name})
        for s in p.findall("send"):
            if s.text:
                res["sends"][s.text.strip()] = pid
    for s in root.iter("stream"):
        sid = s.get("stream_id") or s.get("id") or ""
        label = (s.findtext("label") or "").strip()
        if sid and label:
            res["streams"][label] = sid
    for a in root.iter("participantstreamassoc"):
        pid = a.get("participant_id") or a.get("id") or ""
        for s in a.findall("send"):
            if s.text:
                res["sends"][s.text.strip()] = pid
    return res


# --------------------------------------------------------------------------
# RTP
# --------------------------------------------------------------------------

class Ports:
    def __init__(self):
        self.lock = threading.Lock()
        self.free = list(range(RTP_START - RTP_START % 2, RTP_END + 1, 2))

    def take(self) -> tuple[int, socket.socket] | None:
        with self.lock:
            for port in list(self.free):
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                    s.bind(("0.0.0.0", port))
                except OSError:
                    s.close()
                    continue
                self.free.remove(port)
                return port, s
        return None

    def give(self, port: int) -> None:
        with self.lock:
            if port not in self.free:
                self.free.append(port)


PORTS = Ports()


class Stream:
    """Ein empfangener Sprachstrom. Schreibt die Nutzdaten je Codec in eine Datei und merkt sich,
    an welcher Stelle (Sekunden ab Gespraechsbeginn) jedes zusammenhaengende Stueck beginnt."""

    def __init__(self, call: "Call", index: int, label: str, port: int, sock: socket.socket):
        self.call, self.index, self.label, self.port, self.sock = call, index, label, port, sock
        self.pts: dict[int, str] = {}
        self.files: dict[str, object] = {}
        self.sizes: dict[str, int] = {}
        self.segments: list[dict] = []     # {codec, pos, offset (Bytes in der Codec-Datei), length}
        self.packets = 0
        self.ssrc = None
        self.raw_ts = 0
        self.ext_ts = 0
        self.anchor: tuple[int, float] | None = None
        self.expect = None
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self) -> None:
        self.sock.settimeout(1.0)
        while self.running:
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.packet(data, time.time())
            except Exception as e:  # einzelnes Paket kaputt
                log(f"RTP-Fehler {self.call.id}/{self.index}: {e}")

    def packet(self, data: bytes, now: float) -> None:
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        cc, ext, pad = data[0] & 0x0F, data[0] & 0x10, data[0] & 0x20
        pt = data[1] & 0x7F
        _seq, ts, ssrc = struct.unpack("!HII", data[2:12])
        off = 12 + 4 * cc
        if ext and len(data) >= off + 4:
            off += 4 + 4 * struct.unpack("!H", data[off + 2:off + 4])[0]
        end = len(data) - (data[-1] if pad and data else 0)
        payload = data[off:end]
        name = self.pts.get(pt) or STATIC_PT.get(pt, "")
        codec = CODECS.get(name)
        if not codec or not payload:
            return                      # Tastentoene, Comfort Noise, Unbekanntes
        self.call.last_rtp = now
        pos_now = now - self.call.start
        new_anchor = False
        if ssrc != self.ssrc:
            self.ssrc, self.raw_ts, self.ext_ts = ssrc, ts, 0
            new_anchor = True
        else:
            delta = (ts - self.raw_ts) & 0xFFFFFFFF
            if delta > 0x7FFFFFFF:
                return                  # verspaetetes Paket
            self.raw_ts = ts
            self.ext_ts += delta
        if new_anchor or self.anchor is None:
            self.anchor = (self.ext_ts, pos_now)
        pos = self.anchor[1] + (self.ext_ts - self.anchor[0]) / 8000.0
        if abs(pos - pos_now) > 1.5:     # Zeitstempel springen (Halten, neuer Sender): neu ausrichten
            self.anchor, pos, new_anchor = (self.ext_ts, pos_now), pos_now, True
        seg = self.segments[-1] if self.segments else None
        if (new_anchor or seg is None or seg["codec"] != codec or self.expect != self.ext_ts):
            seg = {"codec": codec, "pos": round(pos, 4), "offset": self.sizes.get(codec, 0), "length": 0}
            self.segments.append(seg)
        f = self.files.get(codec)
        if f is None:
            f = self.files[codec] = open(self.call.tmp / f"s{self.index}-{codec}.raw", "wb")
        f.write(payload)
        self.sizes[codec] = self.sizes.get(codec, 0) + len(payload)
        seg["length"] += len(payload)
        self.expect = self.ext_ts + len(payload)    # G.711/G.722: 1 Byte je 8-kHz-Takt
        self.packets += 1

    def stop(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=3)
        for f in self.files.values():
            f.close()
        PORTS.give(self.port)


# --------------------------------------------------------------------------
# Gespraeche
# --------------------------------------------------------------------------

class Call:
    def __init__(self, msg: Msg):
        self.call_id = msg.call_id
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.tmp = REC_DIR / ".tmp" / self.id
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.start = time.time()
        self.last_rtp = self.start
        self.to_tag = uuid.uuid4().hex[:10]
        self.sess_id = random.randint(10 ** 8, 10 ** 9)
        self.sdp_version = 1
        self.streams: list[Stream] = []
        self.meta = {"participants": [], "streams": {}, "sends": {}}
        self.from_user = user_of(msg.get("from"))
        self.to_user = user_of(msg.get("to"))
        self.ruri_user = user_of(msg.uri)
        self.last: dict[tuple, bytes] = {}
        self.done = False
        self.codec = ""

    def update(self, sdp: str, xml: str) -> bytes | None:
        """Angebot verarbeiten -> SDP-Antwort (None: kein brauchbarer Strom)."""
        if xml:
            m = parse_metadata(xml)
            if m["participants"]:
                self.meta["participants"] = m["participants"]
            self.meta["streams"].update(m["streams"])
            self.meta["sends"].update(m["sends"])
        media = parse_sdp(sdp)
        if not media:
            return None
        lines, usable = [], 0
        for i, m in enumerate(media):
            pick = None
            if m["type"] == "audio" and m["port"]:
                for f in m["fmts"]:
                    if f.isdigit():
                        name = m["rtpmap"].get(int(f)) or STATIC_PT.get(int(f), "")
                        if name in CODECS:
                            pick = (int(f), name)
                            break
            if pick is None:
                lines += [f"m={m['type']} 0 {m['proto']} {' '.join(m['fmts'][:1]) or '0'}"]
                continue
            st = self.streams[i] if i < len(self.streams) else None
            if st is None:
                got = PORTS.take()
                if not got:
                    log(f"{self.id}: keine freien RTP-Ports (RTP_START/RTP_END)")
                    lines += [f"m=audio 0 {m['proto']} {pick[0]}"]
                    continue
                st = Stream(self, i, m["label"], *got)
                self.streams.append(st)
            st.label = m["label"] or st.label
            st.pts = {pt: name for pt, name in m["rtpmap"].items()}
            st.pts.setdefault(pick[0], pick[1])
            self.codec = pick[1].upper()
            te = next((int(f) for f in m["fmts"] if f.isdigit() and m["rtpmap"].get(int(f)) == "telephone-event"),
                      None)
            fmts = f"{pick[0]}" + (f" {te}" if te is not None else "")
            rate = 8000
            lines += [f"m=audio {st.port} {m['proto']} {fmts}", f"a=rtpmap:{pick[0]} {pick[1].upper()}/{rate}"]
            if te is not None:
                lines += [f"a=rtpmap:{te} telephone-event/8000", f"a=fmtp:{te} 0-15"]
            if m["label"]:
                lines.append(f"a=label:{m['label']}")
            lines.append("a=recvonly")
            usable += 1
        if not usable:
            return None
        head = [f"v=0", f"o=cme-recorder {self.sess_id} {self.sdp_version} IN IP4 {HOST_IP}", "s=cme-recorder",
                f"c=IN IP4 {HOST_IP}", "t=0 0"]
        self.sdp_version += 1
        return ("\r\n".join(head + lines) + "\r\n").encode()

    def stop(self) -> None:
        for st in self.streams:
            st.stop()


CALLS: dict[str, Call] = {}
CALLS_LOCK = threading.Lock()
JOBS: Queue = Queue()


def handle(msg: Msg) -> None:
    if not msg.is_request:
        return
    meth = msg.method
    if meth == "ACK":
        return
    if meth == "OPTIONS":
        respond(msg, 200, "OK", to_tag=uuid.uuid4().hex[:8], extra=["Accept: application/sdp"])
        return
    with CALLS_LOCK:
        call = CALLS.get(msg.call_id)
    if meth == "INVITE":
        if call:
            key = msg.cseq
            if key in call.last:                       # Wiederholung
                msg.reply(call.last[key])
                return
            sdp, xml = split_body(msg)
            answer = call.update(sdp, xml) if sdp else None
            if sdp and answer is None:
                respond(msg, 488, "Not Acceptable Here", call.to_tag)
                return
            call.last[key] = respond(msg, 200, "OK", call.to_tag, answer or b"",
                                     "application/sdp" if answer else "")
            return
        if CME_IPS and msg.src[0] not in CME_IPS:
            log(f"Anruf von {msg.src[0]} abgelehnt (nicht CME_IP)")
            respond(msg, 403, "Forbidden", uuid.uuid4().hex[:8])
            return
        if not settings().get("enabled", True):
            respond(msg, 480, "Temporarily Unavailable", uuid.uuid4().hex[:8])
            return
        respond(msg, 100, "Trying")
        sdp, xml = split_body(msg)
        call = Call(msg)
        answer = call.update(sdp, xml)
        if answer is None:
            call.stop()
            shutil.rmtree(call.tmp, ignore_errors=True)
            log(f"Anruf {msg.call_id}: kein unterstuetzter Codec (G.711/G.722) im Angebot")
            respond(msg, 488, "Not Acceptable Here", call.to_tag)
            return
        with CALLS_LOCK:
            CALLS[msg.call_id] = call
        call.last[msg.cseq] = respond(msg, 200, "OK", call.to_tag, answer, "application/sdp")
        parts = ", ".join(p["number"] or p["name"] or "?" for p in call.meta["participants"])
        log(f"Aufzeichnung {call.id} beginnt ({len(call.streams)} Stroeme, {call.codec}"
            + (f", Teilnehmer {parts}" if parts else f", {call.from_user} -> {call.to_user}") + ")")
        return
    if meth == "UPDATE" and call:
        sdp, xml = split_body(msg)
        answer = call.update(sdp, xml) if sdp else None
        respond(msg, 200, "OK", call.to_tag, answer or b"", "application/sdp" if answer else "")
        return
    if meth in ("BYE", "CANCEL"):
        respond(msg, 200, "OK", call.to_tag if call else uuid.uuid4().hex[:8])
        if call:
            finish(call, "BYE" if meth == "BYE" else "CANCEL")
        return
    if meth in ("INFO", "NOTIFY", "UPDATE"):
        respond(msg, 200 if call else 481, "OK" if call else "Call/Transaction Does Not Exist",
                call.to_tag if call else "")
        return
    respond(msg, 405, "Method Not Allowed", uuid.uuid4().hex[:8])


def finish(call: Call, why: str) -> None:
    with CALLS_LOCK:
        if call.done:
            return
        call.done = True
        CALLS.pop(call.call_id, None)
    call.stop()
    call.end = time.time()
    log(f"Aufzeichnung {call.id} endet ({why}), {sum(s.packets for s in call.streams)} Pakete")
    JOBS.put(call)


def watchdog() -> None:
    while True:
        time.sleep(15)
        now = time.time()
        with CALLS_LOCK:
            calls = list(CALLS.values())
        for c in calls:
            if now - c.last_rtp > IDLE_SECS:
                finish(c, "keine Sprachdaten mehr")
            elif now - c.start > MAX_SECS:
                finish(c, "Hoechstdauer erreicht")


def udp_server() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", SIP_PORT))
    while True:
        data, src = s.recvfrom(65535)
        if not data.strip():
            continue                     # Keepalive
        try:
            handle(Msg(data, src, lambda b, src=src: s.sendto(b, src)))
        except Exception as e:
            log(f"SIP-Fehler von {src[0]}: {type(e).__name__}: {e}")


def tcp_server() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", SIP_PORT))
    srv.listen(16)
    while True:
        conn, src = srv.accept()
        threading.Thread(target=tcp_conn, args=(conn, src), daemon=True).start()


def tcp_conn(conn: socket.socket, src: tuple) -> None:
    buf = b""
    lock = threading.Lock()

    def send(b: bytes) -> None:
        with lock:
            try:
                conn.sendall(b)
            except OSError:
                pass
    conn.settimeout(3600)
    try:
        while True:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65535)
                if not chunk:
                    return
                buf += chunk
                buf = buf.lstrip(b"\r\n")
            head, _, rest = buf.partition(b"\r\n\r\n")
            m = re.search(rb"(?im)^(?:content-length|l)\s*:\s*(\d+)", head)
            n = int(m.group(1)) if m else 0
            while len(rest) < n:
                chunk = conn.recv(65535)
                if not chunk:
                    return
                rest += chunk
            data, buf = head + b"\r\n\r\n" + rest[:n], rest[n:]
            try:
                handle(Msg(data, src, send))
            except Exception as e:
                log(f"SIP-Fehler (TCP) von {src[0]}: {type(e).__name__}: {e}")
    except OSError:
        pass
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Auswertung: Audio, Abschrift, Mail
# --------------------------------------------------------------------------

def ffmpeg(args: list[str], data: bytes | None = None, timeout: int = 900) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], input=data, capture_output=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-400:])
    return r.stdout


def channel_pcm(call: Call, st: Stream, rate: int, out: Path) -> float:
    """Stroeme dekodieren und an die richtige Zeitstelle legen -> Sekunden mit Ton."""
    with open(out, "wb") as f:
        for codec, size in st.sizes.items():
            raw = (call.tmp / f"s{st.index}-{codec}.raw").read_bytes()
            inp = ["-f", "g722"] if codec == "g722" else ["-f", codec, "-ar", "8000", "-ac", "1"]
            pcm = ffmpeg([*inp, "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(rate), "pipe:1"], raw)
            per_byte = 2 if codec == "g722" else rate // 8000     # Samples je Eingangs-Byte
            for seg in (s for s in st.segments if s["codec"] == codec):
                a = seg["offset"] * per_byte * 2
                chunk = pcm[a:a + seg["length"] * per_byte * 2]
                f.seek(int(seg["pos"] * rate) * 2)
                f.write(chunk)
    return sum(s["length"] * (2 if s["codec"] == "g722" else 1) for s in st.segments) / (
        16000 if any(s["codec"] == "g722" for s in st.segments) else 8000)


def digits(n: str) -> str:
    return re.sub(r"[^0-9+]", "", n or "")


def classify(call: Call, s: dict) -> dict:
    """Teilnehmer bestimmen: wer ist intern, wer extern, Richtung, Kanal je Strom."""
    internal = {digits(x) for x in s.get("internal", []) if x}
    # eigene Rufnummern (Trunk) zaehlen zur internen Seite; Vergleich ueber die letzten 8 Ziffern
    own = {re.sub(r"\D", "", x)[-8:] for x in s.get("own", []) if len(re.sub(r"\D", "", x)) >= 8}
    prefix = s.get("prefix") or ""

    def clean(n: str) -> str:
        n = digits(n)
        if prefix and n.startswith(prefix) and n[len(prefix):] in internal:
            return n[len(prefix):]
        return n

    def is_int(n: str) -> bool:
        n = clean(n)
        d = re.sub(r"\D", "", n)
        return bool(n) and (n in internal or (len(d) >= 8 and d[-8:] in own)
                            or (len(n) <= 5 and not n.startswith(("+", "0"))))

    parts = [dict(p, number=clean(p["number"])) for p in call.meta["participants"]]
    if not parts:
        parts = [{"id": "from", "number": clean(call.from_user), "name": ""},
                 {"id": "to", "number": clean(call.to_user), "name": ""}]
    ints = [p for p in parts if is_int(p["number"])]
    exts = [p for p in parts if not is_int(p["number"])]
    direction = ""
    if ints and exts:
        direction = "out" if parts.index(ints[0]) < parts.index(exts[0]) else "in"
    # Strom -> Teilnehmer
    by_id = {p["id"]: p for p in parts}
    owner = {}
    for st in call.streams:
        sid = call.meta["streams"].get(st.label, "")
        pid = call.meta["sends"].get(sid) or call.meta["sends"].get(st.label)
        owner[st.index] = by_id.get(pid)
    return {"parts": parts, "internal": ints[0] if ints else None, "external": exts[0] if exts else None,
            "direction": direction, "owner": owner}


def transcribe(wav: Path) -> list[dict]:
    url = os.environ.get("WHISPER_URL", "").rstrip("/")
    if not url:
        raise RuntimeError("WHISPER_URL fehlt")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"{wav.name}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    lang = os.environ.get("WHISPER_LANGUAGE", "de")
    req = urllib.request.Request(
        f"{url}/asr?task=transcribe&language={lang}&output=json&encode=true&vad_filter=true",
        data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=int(os.environ.get("WHISPER_TIMEOUT", "1800"))) as r:
        j = json.loads(r.read().decode("utf-8", "replace") or "{}")
    segs = j.get("segments") or []
    if not segs and j.get("text"):
        segs = [{"start": 0, "text": j["text"]}]
    return [{"start": float(x.get("start") or 0), "text": " ".join(str(x.get("text") or "").split())}
            for x in segs if str(x.get("text") or "").strip()]


def fmt_pos(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def send_mail(to: str, meta: dict, mp3: Path) -> None:
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("SMTP_FROM", "") or user
    if not user:
        raise RuntimeError("SMTP_USER fehlt")
    t = time.localtime(meta["start"])
    dirtext = {"in": "Eingehend", "out": "Ausgehend"}.get(meta["direction"], "")
    other = meta["external"] or "unbekannt"
    msg = EmailMessage()
    msg["From"] = formataddr(("Gesprächsaufzeichnung", sender))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = (f"Gesprächsaufzeichnung {other} ("
                      + (f"{dirtext.lower()}, " if dirtext else "") + f"{time.strftime('%d.%m.%Y %H:%M', t)})")
    lines = [f"{dirtext}es Gespräch" if dirtext else "Gespräch", "",
             f"Datum:   {time.strftime('%d.%m.%Y %H:%M:%S', t)}",
             f"Dauer:   {fmt_pos(meta['duration'])} min",
             f"Intern:  {meta['internal'] or '–'}", f"Extern:  {other}", ""]
    big = mp3.stat().st_size > MAIL_MAX
    if big:
        lines += ["Die Aufnahme ist zu groß für den Anhang, sie liegt in cme-web unter Aufzeichnung.", ""]
    if meta.get("transcript"):
        lines += ["Abschrift (automatisch erstellt, kann Fehler enthalten):", "", meta["transcript"], ""]
    msg.set_content("\n".join(lines))
    if not big:
        msg.add_attachment(mp3.read_bytes(), maintype="audio", subtype="mpeg",
                           filename=f"gespraech-{meta['id']}.mp3")
    host = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    s = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx) if port == 465 else smtplib.SMTP(host, port, timeout=60)
    with s:
        if port != 465:
            s.starttls(context=ctx)
        s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg, from_addr=sender, to_addrs=[to])


def recipient(s: dict, meta: dict) -> str:
    merged = {**(s.get("vm_emails") or {}), **(s.get("emails") or {})}   # leer = Voicemail-Adresse
    emails = {digits(k): v for k, v in merged.items() if v}
    for n in (meta["internal"], meta["external"]):
        if n and digits(n) in emails:
            return emails[digits(n)]
    for n in (meta["internal"], meta["external"]):     # z. B. Durchwahl am Ende der Rufnummer
        for k, v in emails.items():
            if n and len(k) >= 2 and digits(n).endswith(k):
                return v
    return s.get("default_email", "")


def process(call: Call) -> None:
    s = settings()
    try:
        streams = [st for st in call.streams if st.packets]
        if not streams:
            log(f"{call.id}: keine Sprachdaten empfangen, verworfen")
            return
        rate = 16000 if any(seg["codec"] == "g722" for st in streams for seg in st.segments) else 8000
        info = classify(call, s)
        # interne Seite nach links
        if len(streams) > 1 and info["internal"] and info["owner"].get(streams[1].index) is info["internal"]:
            streams = [streams[1], streams[0], *streams[2:]]
        streams = streams[:2]
        pcms = []
        for st in streams:
            p = call.tmp / f"ch{st.index}.s16"
            channel_pcm(call, st, rate, p)
            pcms.append(p)
        size = max(p.stat().st_size for p in pcms)
        for p in pcms:
            with open(p, "r+b") as f:
                f.truncate(size)
        duration = size / 2 / rate
        if duration < float(s.get("min_secs") or 0):
            log(f"{call.id}: nur {duration:.1f} s, verworfen")
            return
        REC_DIR.mkdir(parents=True, exist_ok=True)
        mp3 = REC_DIR / f"{call.id}.mp3"
        inputs = []
        for p in pcms:
            inputs += ["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", str(p)]
        if len(pcms) == 2:
            ffmpeg([*inputs, "-filter_complex", "[0:a][1:a]amerge=inputs=2[a]", "-map", "[a]",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(mp3)])
        else:
            ffmpeg([*inputs, "-c:a", "libmp3lame", "-b:a", "32k", str(mp3)])

        def label(i: int) -> str:
            p = info["owner"].get(streams[i].index) if i < len(streams) else None
            if p is None:
                return "Seite 1" if i == 0 else "Seite 2"
            return p["name"] or p["number"] or ("Intern" if p is info["internal"] else "Extern")

        meta = {"id": call.id, "start": round(call.start, 1), "duration": round(duration, 1),
                "direction": info["direction"],
                "internal": info["internal"]["number"] if info["internal"] else "",
                "external": info["external"]["number"] if info["external"] else "",
                "parties": [{"number": p["number"], "name": p["name"]} for p in info["parts"]],
                "channels": [label(i) for i in range(len(streams))],
                "codec": call.codec, "file": mp3.name, "size": mp3.stat().st_size,
                "transcript": "", "transcript_status": "off", "mailed": "", "mail_error": ""}
        meta_path = REC_DIR / f"{call.id}.json"

        def save() -> None:
            tmp = meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
            tmp.replace(meta_path)
        if s.get("transcript"):
            meta["transcript_status"] = "pending"
        save()
        log(f"{call.id}: {fmt_pos(duration)} min gespeichert ({meta['internal'] or '?'} / {meta['external'] or '?'})")

        if s.get("transcript"):
            try:
                t0, rows = time.time(), []
                for i, p in enumerate(pcms):
                    wav = call.tmp / f"ch{i}.wav"
                    ffmpeg(["-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", str(p), "-ar", "16000", str(wav)])
                    rows += [(x["start"], i, x["text"]) for x in transcribe(wav)]
                rows.sort()
                out, last = [], None
                for start, i, text in rows:
                    if out and last == i:
                        out[-1] += " " + text
                    else:
                        out.append(f"[{fmt_pos(start)}] {meta['channels'][i]}: {text}")
                    last = i
                meta["transcript"], meta["transcript_status"] = "\n".join(out), "ok"
                log(f"{call.id}: Abschrift in {time.time() - t0:.0f} s")
            except Exception as e:
                meta["transcript_status"] = "failed"
                log(f"{call.id}: Abschrift fehlgeschlagen: {type(e).__name__}: {e}")
            save()

        if s.get("mail"):
            to = recipient(s, meta)
            if to:
                try:
                    send_mail(to, meta, mp3)
                    meta["mailed"] = to
                    log(f"{call.id}: Mail an {to}")
                except Exception as e:
                    meta["mail_error"] = f"{type(e).__name__}: {e}"[:300]
                    log(f"{call.id}: Mail an {to} fehlgeschlagen: {meta['mail_error']}")
                save()
    except Exception as e:
        log(f"{call.id}: Auswertung fehlgeschlagen: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(call.tmp, ignore_errors=True)


def worker() -> None:
    while True:
        process(JOBS.get())


def housekeeping() -> None:
    """Statusdatei fuer cme-web, alte Aufnahmen und Reste loeschen."""
    last_clean = 0.0
    while True:
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            with CALLS_LOCK:
                active = [{"id": c.id, "start": c.start, "streams": len(c.streams)} for c in CALLS.values()]
            st = {"time": time.time(), "version": VERSION, "host_ip": HOST_IP, "sip_port": SIP_PORT,
                  "rtp": f"{RTP_START}-{RTP_END}", "active": active, "queue": JOBS.qsize(), "log": list(LOG)[-25:]}
            tmp = REC_DIR / ".status.json.tmp"
            tmp.write_text(json.dumps(st, ensure_ascii=False))
            tmp.replace(REC_DIR / ".status.json")
            if time.time() - last_clean > 3600:
                last_clean = time.time()
                days = float(settings().get("retention_days") or 0)
                if days > 0:
                    limit = time.time() - days * 86400
                    n = 0
                    for meta in REC_DIR.glob("*.json"):
                        try:
                            start = json.loads(meta.read_text()).get("start", meta.stat().st_mtime)
                        except ValueError:
                            start = meta.stat().st_mtime
                        if start < limit:
                            for f in REC_DIR.glob(meta.stem + ".*"):
                                f.unlink(missing_ok=True)
                            n += 1
                    if n:
                        log(f"{n} Aufnahmen älter als {days:g} Tage gelöscht")
                for d in (REC_DIR / ".tmp").glob("*") if (REC_DIR / ".tmp").is_dir() else []:
                    with CALLS_LOCK:
                        busy = any(c.tmp == d for c in CALLS.values())
                    if not busy and time.time() - d.stat().st_mtime > 86400:
                        shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            log(f"Aufräumen: {type(e).__name__}: {e}")
        time.sleep(20)


def main() -> None:
    if not HOST_IP:
        print("HOST_IP (VM_HOST_IP) fehlt - Aufzeichnung nicht eingerichtet", flush=True)
        while True:
            time.sleep(3600)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg fehlt")
    # Reste eines Absturzes verwerfen
    shutil.rmtree(REC_DIR / ".tmp", ignore_errors=True)
    for fn in (udp_server, tcp_server, watchdog, worker, housekeeping):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    log(f"cme-recorder {VERSION}: SIP {HOST_IP}:{SIP_PORT} (UDP/TCP), RTP {RTP_START}-{RTP_END}, "
        f"nur von {', '.join(sorted(CME_IPS)) or 'allen'}")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
