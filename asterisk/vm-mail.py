#!/usr/bin/env python3
"""
vm-mail.py - Versand der Voicemail-Mails (mailcmd von app_voicemail)

Asterisk uebergibt die fertige Mail (mit WAV-Anhang) auf stdin. Das Skript schickt die
Aufnahme an den Whisper-Container (WHISPER_URL), setzt die Abschrift an der Stelle
@@ABSCHRIFT@@ in den Text ein und verschickt die Mail per SMTP (z.B. iCloud).
Klappt die Abschrift nicht, geht die Mail trotzdem raus.

Umgebung: SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = TLS), SMTP_USER, SMTP_PASSWORD,
SMTP_FROM (Absender, Standard SMTP_USER), WHISPER_URL, WHISPER_LANGUAGE (Standard de).
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
import time
import urllib.request
import uuid
from email import policy
from email.parser import BytesParser
from email.utils import formataddr, getaddresses

MARKER = "@@ABSCHRIFT@@"
LOG = "/var/log/asterisk/vm-mail.log"


def log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} vm-mail: {text}"
    print(line, file=sys.stderr, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def transcribe(audio: bytes, filename: str) -> str:
    url = os.environ.get("WHISPER_URL", "").rstrip("/")
    if not url:
        return ""
    lang = os.environ.get("WHISPER_LANGUAGE", "de")
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio_file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{url}/asr?task=transcribe&language={lang}&output=txt&encode=true&vad_filter=true",
        data=body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=int(os.environ.get("WHISPER_TIMEOUT", "600"))) as r:
        text = r.read().decode("utf-8", "replace").strip()
    log(f"Abschrift in {time.time() - t0:.1f} s, {len(text)} Zeichen")
    return " ".join(text.split())


def main() -> int:
    msg = BytesParser(policy=policy.default).parsebytes(sys.stdin.buffer.read())
    audio, fname, textpart = None, "nachricht.wav", None
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename() or ""
        if part.get_content_maintype() == "audio" or name.lower().endswith(".wav"):
            audio, fname = part.get_payload(decode=True), name or fname
        elif part.get_content_type() == "text/plain" and textpart is None:
            textpart = part

    transcript = ""
    if audio:
        try:
            transcript = transcribe(audio, fname)
        except Exception as e:  # Mail trotzdem verschicken
            log(f"Abschrift fehlgeschlagen: {type(e).__name__}: {e}")
    if textpart is not None:
        body = textpart.get_content()
        if MARKER in body:
            body = body.replace(MARKER, transcript or "(keine Abschrift verfuegbar)")
        elif transcript:
            body = f"{body.rstrip()}\n\nAbschrift:\n{transcript}\n"
        textpart.set_content(body, charset="utf-8")

    user = os.environ.get("SMTP_USER", "")
    sender = os.environ.get("SMTP_FROM", "") or user
    if not (user and sender):
        log("SMTP_USER fehlt, Mail nicht verschickt")
        return 1
    del msg["From"]
    msg["From"] = formataddr(("Voicemail", sender))
    rcpts = [a for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", [])) if a]
    if not rcpts:
        log("Kein Empfaenger in der Mail")
        return 1

    host = os.environ.get("SMTP_HOST", "smtp.mail.me.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=60, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=60)
            s.starttls(context=ctx)
        with s:
            s.login(user, os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg, from_addr=sender, to_addrs=rcpts)
    except Exception as e:
        log(f"Versand an {', '.join(rcpts)} fehlgeschlagen: {type(e).__name__}: {e}")
        return 1
    log(f"Mail an {', '.join(rcpts)} verschickt" + (" (mit Abschrift)" if transcript else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
