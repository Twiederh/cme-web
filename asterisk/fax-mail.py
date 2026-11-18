#!/usr/bin/env python3
"""
fax-mail.py - empfangenes Fax als PDF ablegen und per Mail verschicken

Aufruf aus dem Dialplan (Kontext cme-fax, Extension h):
  fax-mail.py <datei ohne Endung> <email> <status> <seiten> <fehler> <absender-kennung>
              <anrufer> <faxnummer> <name>

Legt <datei>.pdf und <datei>.json (fuer die Weboberflaeche) ab und loescht das TIFF.
SMTP-Zugang wie bei vm-mail.py (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM).
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path

LOG = "/var/log/asterisk/vm-mail.log"


def log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} fax-mail: {text}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def send(msg: EmailMessage, rcpt: str) -> None:
    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("SMTP_FROM", "") or user
    if not user:
        raise RuntimeError("SMTP_USER fehlt")
    msg["From"] = formataddr(("Fax", sender))
    host = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=60)
        s.starttls(context=ctx)
    with s:
        s.login(user, os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg, from_addr=sender, to_addrs=[rcpt])


def main(argv: list[str]) -> int:
    args = (argv + [""] * 9)[:9]
    base, email, status, pages, error, station, caller, faxnum, name = args
    if not base:
        log("Aufruf ohne Dateiname")
        return 1
    tif, pdf, meta = Path(base + ".tif"), Path(base + ".pdf"), Path(base + ".json")
    ok = status == "SUCCESS" and tif.exists()
    if tif.exists():
        r = subprocess.run(["tiff2pdf", "-o", str(pdf), "-t", f"Fax von {caller or station or 'unbekannt'}", str(tif)],
                           capture_output=True)
        if r.returncode == 0 and pdf.exists():
            tif.unlink(missing_ok=True)
        else:
            log(f"tiff2pdf fehlgeschlagen: {r.stderr.decode(errors='replace')[-200:]}")
    info = {"time": int(time.time()), "number": faxnum, "name": name, "caller": caller, "station": station.strip(),
            "pages": int(pages) if pages.isdigit() else 0, "status": status or "FAILED", "error": error,
            "email": email, "mailed": False}
    if not ok and not pdf.exists():
        log(f"Fax fuer {faxnum} von {caller} nicht empfangen: {status} {error}")
    if email:
        msg = EmailMessage()
        msg["To"] = email
        msg["Date"] = formatdate(localtime=True)
        who = caller or station or "unbekannt"
        if pdf.exists():
            msg["Subject"] = f"Fax von {who} ({info['pages']} Seite{'n' if info['pages'] != 1 else ''})"
            body = (f"Neues Fax an {name or faxnum}.\n\nAbsender: {who}\n"
                    + (f"Kennung: {station}\n" if station and station != caller else "")
                    + f"Seiten: {info['pages']}\nEmpfangen: {time.strftime('%d.%m.%Y um %H:%M')}\n"
                    + ("" if ok else f"\nDer Empfang war unvollstaendig ({status} {error}).\n")
                    + "\nDas Fax ist als PDF angehaengt.\n")
            msg.set_content(body)
            msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf",
                               filename=f"Fax-{time.strftime('%Y-%m-%d-%H%M')}.pdf")
        else:
            msg["Subject"] = f"Fax von {who} nicht empfangen"
            msg.set_content(f"Ein Fax an {name or faxnum} von {who} konnte nicht empfangen werden.\n\n"
                            f"Status: {status or 'unbekannt'} {error}\n")
        try:
            send(msg, email)
            info["mailed"] = True
            log(f"Fax-Mail an {email} verschickt ({status}, {info['pages']} Seiten)")
        except Exception as e:
            log(f"Fax-Mail an {email} fehlgeschlagen: {type(e).__name__}: {e}")
    meta.write_text(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
