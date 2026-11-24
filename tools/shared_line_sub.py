#!/usr/bin/env python3
"""Shared-Line-Abo stellvertretend für ein Gerät, das selbst keins schickt (z.B. Gigaset).

CME (vcube) nimmt ein SIP-Gerät nur dann in eine Shared Line auf ("show shared-line details"), wenn es die
Leitung mit SUBSCRIBE Event: dialog abonniert. Den Teilnehmer führt CME unter dem From-URI des Abos und sucht
den Dial-Peer über dessen Host – ein Gerätename im Contact ist nicht nötig (getestet mit CME Phone für Mac).

Dieses Skript schickt das Abo mit der IP des eigentlichen Geräts im From, bestätigt die NOTIFYs und erneuert
das Abo. Das Gerät selbst muss für die Nummer am CME angemeldet sein (Dial-Peer mit seiner IP als Ziel);
die INVITEs schickt CME dann direkt dorthin.

  python3 tools/shared_line_sub.py --cme 192.168.11.4 --number +4961874192090 --device-ip 192.168.7.213

Strg-C beendet das Abo (SUBSCRIBE mit Expires 0).
"""
import argparse
import hashlib
import random
import re
import socket
import string
import sys
import time


def rnd(n=10):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def header(msg, name):
    m = re.search(rf"^{name}\s*:\s*(.*)$", msg, re.I | re.M)
    return m.group(1).strip() if m else ""


def digest(auth_hdr, method, uri, user, password):
    p = dict(re.findall(r'(\w+)="?([^",]*)"?', auth_hdr))
    ha1 = hashlib.md5(f"{user}:{p['realm']}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    if "auth" in p.get("qop", ""):
        cnonce, nc = rnd(16), "00000001"
        resp = hashlib.md5(f"{ha1}:{p['nonce']}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
        return (f'Digest username="{user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", '
                f'response="{resp}", algorithm=MD5, cnonce="{cnonce}", nc={nc}, qop=auth')
    resp = hashlib.md5(f"{ha1}:{p['nonce']}:{ha2}".encode()).hexdigest()
    return (f'Digest username="{user}", realm="{p["realm"]}", nonce="{p["nonce"]}", uri="{uri}", '
            f'response="{resp}", algorithm=MD5')


class Sub:
    def __init__(self, a):
        self.a = a
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", a.port))
        self.sock.settimeout(1.0)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect((a.cme, 5060))
        self.local = probe.getsockname()[0]
        probe.close()
        self.call_id = rnd(16)
        self.tag = rnd(9)
        self.to_tag = ""
        self.target = f"sip:{a.number}@{a.cme}"
        self.cseq = 20
        self.auth = ""

    def log(self, text):
        print(time.strftime("%H:%M:%S"), text, flush=True)

    def send(self, data):
        self.sock.sendto(data.encode(), (self.a.cme, 5060))

    def subscribe(self, expires):
        self.cseq += 1
        to = f"<sip:{self.a.number}@{self.a.cme}>" + (f";tag={self.to_tag}" if self.to_tag else "")
        lines = [
            f"SUBSCRIBE {self.target} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local}:{self.a.port};branch=z9hG4bK{rnd()};rport",
            f"From: <sip:{self.a.number}@{self.a.device_ip}>;tag={self.tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} SUBSCRIBE",
            "Max-Forwards: 70",
            "Event: dialog",
            "Accept: application/dialog-info+xml",
            f"Expires: {expires}",
            f"Contact: <sip:{self.a.number}@{self.local}:{self.a.port};transport=udp>",
            "Allow: ACK,BYE,CANCEL,INVITE,NOTIFY,OPTIONS,REFER,REGISTER,UPDATE,SUBSCRIBE",
            "User-Agent: cme-shared-line",
        ]
        if self.auth:
            lines.append(f"Authorization: {self.auth}")
        self.send("\r\n".join(lines + ["Content-Length: 0", "", ""]))

    def reply_ok(self, req):
        keep = [l for l in req.split("\r\n")
                if re.match(r"(Via|From|To|Call-ID|CSeq)\s*:", l, re.I)]
        self.send("\r\n".join(["SIP/2.0 200 OK"] + keep + ["Content-Length: 0", "", ""]))

    def wait_response(self, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            msg = self.recv()
            if msg and msg.startswith("SIP/2.0") and header(msg, "Call-ID") == self.call_id \
                    and "SUBSCRIBE" in header(msg, "CSeq"):
                return msg
        return ""

    def recv(self):
        try:
            data, _ = self.sock.recvfrom(65535)
        except socket.timeout:
            return ""
        msg = data.decode(errors="replace")
        if msg.startswith("NOTIFY"):
            self.reply_ok(msg)
            body = msg.split("\r\n\r\n", 1)[1].strip() if "\r\n\r\n" in msg else ""
            self.log(f"NOTIFY {header(msg, 'Event')} {header(msg, 'Subscription-State')}"
                     + (f" ({len(body)} Bytes)" if body else ""))
        elif not msg.startswith("SIP/2.0"):
            # andere Anfragen (OPTIONS usw.) freundlich bestätigen
            self.reply_ok(msg)
        return msg

    def run_once(self, expires):
        for _ in range(2):
            self.subscribe(expires)
            resp = self.wait_response()
            if not resp:
                self.log("keine Antwort von CME")
                return False
            code = resp.split()[1]
            if code in ("401", "407") and self.a.user:
                hdr = header(resp, "WWW-Authenticate") or header(resp, "Proxy-Authenticate")
                self.auth = digest(hdr, "SUBSCRIBE", self.target, self.a.user, self.a.password)
                continue
            if code.startswith("2"):
                m = re.search(r";\s*tag=([^;>\s]+)", header(resp, "To"))
                if m:
                    self.to_tag = m.group(1)
                c = re.search(r"<([^>]+)>", header(resp, "Contact"))
                if c:
                    self.target = c.group(1)
                self.log(f"SUBSCRIBE {expires}s: {code} (From +…@{self.a.device_ip})")
                return True
            self.log(f"SUBSCRIBE {expires}s abgelehnt: {resp.splitlines()[0]}")
            return False
        return False

    def run(self):
        if not self.run_once(self.a.expires):
            sys.exit(1)
        next_refresh = time.time() + self.a.expires * 0.8
        try:
            while True:
                self.recv()
                if time.time() >= next_refresh:
                    if not self.run_once(self.a.expires):
                        # neu aufsetzen
                        self.call_id, self.tag, self.to_tag = rnd(16), rnd(9), ""
                        self.target = f"sip:{self.a.number}@{self.a.cme}"
                        self.run_once(self.a.expires)
                    next_refresh = time.time() + self.a.expires * 0.8
        except KeyboardInterrupt:
            self.run_once(0)
            self.log("Abo beendet")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cme", required=True, help="IP der CME, z.B. 192.168.11.4")
    ap.add_argument("--number", required=True, help="Nummer der Shared Line, z.B. +4961874192090")
    ap.add_argument("--device-ip", required=True, help="IP des Geräts, das mitklingeln soll (Ziel seines Dial-Peers)")
    ap.add_argument("--port", type=int, default=5070, help="lokaler UDP-Port (Standard 5070)")
    ap.add_argument("--expires", type=int, default=600)
    ap.add_argument("--user", default="", help="nur falls CME das Abo authentifiziert")
    ap.add_argument("--password", default="")
    Sub(ap.parse_args()).run()
