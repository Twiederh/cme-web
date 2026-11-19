"""Visual Voicemail am Telefon: Cisco-IP-Phone-Dienst (XML) fuer die Asterisk-Mailboxen.

Nachbau der Visual Voicemail von Unity Connection, so weit das mit den XML-Diensten der
Cisco-Telefone geht:
  - Liste der Nachrichten (neu/alt) mit Anrufer, Zeit und Dauer
  - Abspielen direkt am Telefon (RTP-Strom an das Telefon, RTPRx), Stopp
  - Rueckruf, Loeschen, als gehoert/ungehoert markieren
Das Telefon wird an seiner IP-Adresse erkannt (show voice register pool all brief); es sieht nur
die Mailboxen seiner eigenen Leitungen.

Die Telefone sprechen nur einfaches HTTP ohne Anmeldung. Deshalb laeuft der Dienst auf einem
eigenen Port (CME_PHONE_PORT, Standard 8081), der nur die Pfade /ipphone/... ausliefert.
Ueber den normalen Port (Traefik/Login) sind diese Pfade gesperrt.

Wird aus voicemail.register() eingehaengt; Oberflaeche: static/vvm.js (Tab Voicemail).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from flask import Response, jsonify, request, send_file

BASE = Path(__file__).resolve().parent
PHONE_PORT = int(os.environ.get("CME_PHONE_PORT", "8081"))                 # Port im Container
URL_PORT = int(os.environ.get("CME_PHONE_URL_PORT") or PHONE_PORT)          # Port auf dem Docker-Host
RTP_PORT = int(os.environ.get("VVM_RTP_PORT", "20480"))
PREFIX = "/ipphone/vvm"
RE_ID = re.compile(r"^[\w.-]{1,64}$")
RE_IP = r"\d+\.\d+\.\d+\.\d+"


# --------------------------------------------------------------------------
# XML fuer die Telefone
# --------------------------------------------------------------------------

def _keys(softkeys) -> str:
    return "".join(f"<SoftKeyItem><Name>{escape(n)}</Name><URL>{escape(u)}</URL><Position>{i}</Position></SoftKeyItem>"
                   for i, (n, u) in enumerate(softkeys, 1))


def xml_menu(title: str, prompt: str, items, softkeys) -> str:
    body = "".join(f"<MenuItem><Name>{escape(n[:64])}</Name><URL>{escape(u)}</URL></MenuItem>" for n, u in items[:100])
    return (f'<CiscoIPPhoneMenu><Title>{escape(title)}</Title><Prompt>{escape(prompt)}</Prompt>'
            f"{body}{_keys(softkeys)}</CiscoIPPhoneMenu>")


def xml_text(title: str, prompt: str, text: str, softkeys) -> str:
    return (f"<CiscoIPPhoneText><Title>{escape(title)}</Title><Prompt>{escape(prompt)}</Prompt>"
            f"<Text>{escape(text)}</Text>{_keys(softkeys)}</CiscoIPPhoneText>")


def xml_execute(*urls: str) -> str:
    return ("<CiscoIPPhoneExecute>" + "".join(f"<ExecuteItem Priority=\"0\" URL={quoteattr(u)}/>" for u in urls)
            + "</CiscoIPPhoneExecute>")


def xml_resp(body: str, refresh: str | None = None) -> Response:
    r = Response('<?xml version="1.0" encoding="UTF-8"?>\n' + body, mimetype="text/xml")
    r.headers["Content-Type"] = "text/xml; charset=utf-8"
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Expires"] = "-1"
    if refresh:
        r.headers["Refresh"] = refresh
    return r


def dial_number(caller: str) -> str:
    """Anrufernummer in eine waehlbare Nummer (mit Amtsholung 0) umwandeln."""
    n = re.sub(r"[^\d+*#]", "", caller or "")
    if n.startswith("+49"):
        return "00" + n[3:]
    if n.startswith("+"):
        return "000" + n[1:]
    return n


def fmt_dur(s: int) -> str:
    return f"{s // 60}:{s % 60:02d}"


def fmt_time(ts: int) -> str:
    if not ts:
        return "?"
    d = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    if d.date() == today:
        return "heute " + d.strftime("%H:%M")
    if (today - d.date()).days == 1:
        return "gestern " + d.strftime("%H:%M")
    return d.strftime("%d.%m. %H:%M")


def parse_pool_ips(out: str) -> dict[str, list[str]]:
    """show voice register pool all brief -> {ip: [Rufnummern]}"""
    res: dict[str, list[str]] = {}
    last = None
    mac_re = r"[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}"
    for line in out.splitlines():
        m = re.match(rf"^\s*(\d+)\s+(?:{mac_re}|{RE_IP}|[A-Za-z][\w-]+)\s+(?:({RE_IP})\s+)?\d+\s+\d+\s+(\S+)\s+\S+", line)
        if m:
            last = m.group(2) or None
            if last:
                res.setdefault(last, []).append(m.group(3))
            continue
        m = re.match(r"^\s+\d+\s+\d+\s+(\S+)\s+\S+\s*$", line)
        if m and last:
            res[last].append(m.group(1))
    return res


# --------------------------------------------------------------------------
# Einhaengen
# --------------------------------------------------------------------------

def register(app, router, demo: bool, ctx: dict) -> None:
    spool: Path = ctx["spool"]
    list_messages = ctx["list_messages"]
    msg_action = ctx["msg_action"]
    settings = ctx["settings"]
    mbid = ctx["mbid"]
    host_ip = ctx["host_ip"]
    players: dict[str, subprocess.Popen] = {}
    plock = threading.Lock()
    cache = {"t": 0.0, "map": {}}

    # ------------- Telefon -> Mailboxen -------------

    def phone_map() -> dict[str, list[str]]:
        if time.time() - cache["t"] > 20:
            cache["map"] = parse_pool_ips(router.show("show voice register pool all brief"))
            cache["t"] = time.time()
        return cache["map"]

    def my_boxes(ip: str) -> list[str]:
        boxes = settings()["mailboxes"]
        pm = phone_map()
        nums = pm.get(ip)
        if nums is None and demo:
            nums = next(iter(pm.values()), [])
        return [n for n in dict.fromkeys(nums or []) if n in boxes]

    def phone_ip() -> str:
        return request.remote_addr or ""

    def base() -> str:
        return request.host_url.rstrip("/") + PREFIX

    def guard(box: str | None = None):
        """Nur ueber den Telefon-Port und nur fuer die eigenen Mailboxen."""
        if request.environ.get("cme.phone") != "1":
            return Response("Nicht gefunden", status=404)
        if not ctx["enabled"]:
            return xml_resp(xml_text("Voicemail", "", "Die Voicemail ist nicht eingerichtet.", [("Beenden", "SoftKey:Exit")]))
        if box is not None and (not RE_ID.match(box) or box not in my_boxes(phone_ip())):
            return xml_resp(xml_text("Voicemail", "", "Diese Mailbox gehört nicht zu diesem Telefon.",
                                     [("Zurück", "SoftKey:Exit")]))
        return None

    def find(box: str, mid: str) -> dict | None:
        return next((m for m in list_messages(spool, mbid(box)) if m["id"] == mid), None)

    def who(m: dict) -> str:
        return m["caller_name"] or m["caller"] or "Unbekannt"

    # ------------- Abspielen (RTP an das Telefon) -------------

    def stop_player(ip: str) -> None:
        with plock:
            p = players.pop(ip, None)
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
                p.kill()

    def start_player(ip: str, wav: Path, after=None) -> None:
        """Aufnahme als G.711 u-law (20-ms-Pakete) per RTP an das Telefon schicken."""
        stop_player(ip)

        def run():
            time.sleep(0.6)     # dem Telefon Zeit geben, den Empfang zu oeffnen
            p = subprocess.Popen(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-i", str(wav),
                 "-af", "aresample=8000,adelay=300,asetnsamples=n=160:p=0", "-ac", "1", "-ar", "8000",
                 "-acodec", "pcm_mulaw", "-payload_type", "0", "-f", "rtp", f"rtp://{ip}:{RTP_PORT}?pkt_size=172"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with plock:
                players[ip] = p
            if after:
                time.sleep(1)   # ffmpeg hat die Datei geoeffnet; jetzt darf sie umbenannt werden
                try:
                    after()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()

    # ------------- Seiten -------------

    def box_menu(box: str, note: str = "") -> Response:
        msgs = list_messages(spool, mbid(box))
        new = sum(m["folder"] == "INBOX" for m in msgs)
        items = [(f"{'● ' if m['folder'] == 'INBOX' else '   '}{fmt_time(m['time'])}  {who(m)}  ({fmt_dur(m['duration'])})",
                  f"{base()}/{box}/{m['id']}") for m in msgs if m["id"]]
        prompt = note or (f"{new} neu, {len(msgs) - new} alt" if msgs else "Keine Nachrichten")
        keys = [("Auswahl", "SoftKey:Select"), ("Mailbox", f"Dial:{settings().get('pilot', '')}"),
                ("Aktual.", f"{base()}/{box}"), ("Beenden", "SoftKey:Exit")]
        if not items:
            return xml_resp(xml_text(f"Voicemail {box}", prompt, "Keine Nachrichten vorhanden.", keys[1:]))
        return xml_resp(xml_menu(f"Voicemail {box}", prompt, items, keys))

    def msg_page(box: str, m: dict, note: str = "") -> Response:
        u = f"{base()}/{box}/{m['id']}"
        lines = [f"Von: {who(m)}"]
        if m["caller_name"] and m["caller"]:
            lines.append(f"Nummer: {m['caller']}")
        lines += [f"Zeit: {datetime.fromtimestamp(m['time']).strftime('%d.%m.%Y %H:%M') if m['time'] else '?'}",
                  f"Dauer: {fmt_dur(m['duration'])}",
                  "Status: " + ("neu" if m["folder"] == "INBOX" else "gehört")]
        keys = [("Abspielen", f"{u}/play"), ("Stopp", f"{u}/stop")]
        if dial_number(m["caller"]):
            keys.append(("Rückruf", f"Dial:{dial_number(m['caller'])}"))
        keys += [("Löschen", f"{u}/delete"),
                 ("Ungehört" if m["folder"] == "Old" else "Gehört", f"{u}/toggle"),
                 ("Zurück", f"{base()}/{box}")]
        return xml_resp(xml_text(who(m)[:32], note or f"Voicemail {box}", "\n".join(lines), keys))

    @app.get(PREFIX)
    def vvm_root():
        g = guard()
        if g:
            return g
        boxes = my_boxes(phone_ip())
        if not boxes:
            return xml_resp(xml_text("Voicemail", "", f"Für dieses Telefon ({phone_ip()}) ist keine Mailbox eingerichtet.",
                                     [("Beenden", "SoftKey:Exit")]))
        if len(boxes) == 1:
            return box_menu(boxes[0])
        items = []
        for b in boxes:
            msgs = list_messages(spool, mbid(b))
            new = sum(m["folder"] == "INBOX" for m in msgs)
            items.append((f"{b}  –  {new} neu, {len(msgs) - new} alt", f"{base()}/{b}"))
        return xml_resp(xml_menu("Voicemail", "Mailbox wählen", items,
                                 [("Auswahl", "SoftKey:Select"), ("Beenden", "SoftKey:Exit")]))

    @app.get(f"{PREFIX}/<box>")
    def vvm_box(box):
        return guard(box) or box_menu(box)

    @app.get(f"{PREFIX}/<box>/<mid>")
    def vvm_msg(box, mid):
        g = guard(box)
        if g:
            return g
        m = find(box, mid) if RE_ID.match(mid) else None
        return msg_page(box, m) if m else box_menu(box, "Nachricht nicht mehr vorhanden")

    @app.get(f"{PREFIX}/<box>/<mid>/play")
    def vvm_play(box, mid):
        g = guard(box)
        if g:
            return g
        m = find(box, mid) if RE_ID.match(mid) else None
        if not m:
            return box_menu(box, "Nachricht nicht mehr vorhanden")
        wav = spool / "default" / mbid(box) / m["folder"] / (m["file"] + ".wav")
        if not wav.exists():
            return msg_page(box, m, "Keine Aufnahme gefunden")
        ip = phone_ip()
        mark = (lambda: msg_action(mbid(box), m["folder"], m["file"], {"Action": "VoicemailMove", "ToFolder": "Old"})) \
            if m["folder"] == "INBOX" else None
        start_player(ip, wav, mark)
        return xml_resp(xml_execute(f"RTPRx:{ip}:{RTP_PORT}"))

    @app.get(f"{PREFIX}/<box>/<mid>/stop")
    def vvm_stop(box, mid):
        g = guard(box)
        if g:
            return g
        stop_player(phone_ip())
        return xml_resp(xml_execute("RTPRx:Stop"))

    @app.get(f"{PREFIX}/<box>/<mid>/delete")
    def vvm_delete(box, mid):
        g = guard(box)
        if g:
            return g
        m = find(box, mid) if RE_ID.match(mid) else None
        if not m:
            return box_menu(box, "Nachricht nicht mehr vorhanden")
        stop_player(phone_ip())
        try:
            msg_action(mbid(box), m["folder"], m["file"], {"Action": "VoicemailRemove"})
            note = "Nachricht gelöscht"
        except Exception as e:
            note = f"Fehler: {e}"[:60]
        return box_menu(box, note)

    @app.get(f"{PREFIX}/<box>/<mid>/toggle")
    def vvm_toggle(box, mid):
        g = guard(box)
        if g:
            return g
        m = find(box, mid) if RE_ID.match(mid) else None
        if not m:
            return box_menu(box, "Nachricht nicht mehr vorhanden")
        to = "Old" if m["folder"] == "INBOX" else "INBOX"
        try:
            msg_action(mbid(box), m["folder"], m["file"], {"Action": "VoicemailMove", "ToFolder": to})
        except Exception as e:
            return msg_page(box, m, f"Fehler: {e}"[:60])
        m2 = find(box, mid) or m
        return msg_page(box, m2, "Als gehört markiert" if to == "Old" else "Als neu markiert")

    # ------------- Weboberflaeche -------------

    def service_url() -> str:
        return f"http://{host_ip or '<docker-host>'}:{URL_PORT}{PREFIX}"

    @app.get("/static/vvm.js")
    def vvm_js():
        return send_file(BASE / "static" / "vvm.js", mimetype="text/javascript", max_age=0)

    @app.get("/api/vvm")
    def vvm_info():
        pc = app.config.get("PHONECFG")
        state = pc["state"](["device/servicesURL[1]", "device/messagesURL[1]"]) if pc else {}
        return jsonify(ok=True, enabled=bool(ctx["enabled"]), url=service_url(), port=URL_PORT,
                       rtp_port=RTP_PORT, host_ip=host_ip, phonecfg=bool(pc), state=state)

    @app.post("/api/vvm/install")
    def vvm_install():
        pc = app.config.get("PHONECFG")
        if not pc:
            return jsonify(ok=False, error="Telefon-Konfiguration nicht verfügbar"), 400
        if not host_ip:
            return jsonify(ok=False, error="VM_HOST_IP ist nicht gesetzt – die Telefone kennen sonst die Adresse nicht."), 400
        d = request.get_json(silent=True) or {}
        url = service_url()
        rules = {"device/servicesURL[1]": url if d.get("services") else None,
                 "device/messagesURL[1]": url if d.get("messages") else None}
        res = pc["set_all"](rules, bool(d.get("restart")))
        return jsonify(ok=True, url=url, **res)

    # ------------- eigener Port fuer die Telefone -------------

    def phone_only(environ, start_response):
        if not environ.get("PATH_INFO", "").startswith("/ipphone/"):
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"Nicht gefunden"]
        environ["cme.phone"] = "1"
        return app.wsgi_app(environ, start_response)

    def serve_phones():
        try:
            from waitress import serve
            serve(phone_only, host="0.0.0.0", port=PHONE_PORT, threads=4, ident="cme-web-phone")
        except ImportError:
            from werkzeug.serving import make_server
            make_server("0.0.0.0", PHONE_PORT, phone_only, threaded=True).serve_forever()
        except OSError as e:
            print(f"Telefon-Dienste: Port {PHONE_PORT} nicht verfügbar ({e})", flush=True)

    if os.environ.get("CME_PHONE_PORT", "") != "0":
        threading.Thread(target=serve_phones, daemon=True, name="phone-services").start()
        print(f"Telefon-Dienste (Visual Voicemail): http://0.0.0.0:{PHONE_PORT}{PREFIX}", flush=True)
