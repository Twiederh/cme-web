#!/usr/bin/env python3
"""Zertifikat für den TLS-Port aus der acme.json von Traefik holen (Let's Encrypt, DNS-Challenge bei Traefik).

Aufruf: acme2pem.py <acme.json> <domain> <zielverzeichnis>
Schreibt cert.pem (mit Kette) und key.pem, gibt "changed" oder "unchanged" aus.
Exit 0 = geschrieben (oder unverändert), 1 = Datei fehlt / kein Zertifikat für die Domain.
"""
import base64
import json
import os
import sys
from pathlib import Path


def find(acme: dict, domain: str):
    for resolver in acme.values():
        for c in (resolver or {}).get("Certificates") or []:
            d = c.get("domain") or {}
            names = [d.get("main", "")] + list(d.get("sans") or [])
            if any(n.lower() == domain or (n.startswith("*.") and domain.endswith(n[1:].lower())) for n in names):
                return c
    return None


def main() -> int:
    src, domain, out = Path(sys.argv[1]), sys.argv[2].lower(), Path(sys.argv[3])
    try:
        c = find(json.loads(src.read_text()), domain)
    except (OSError, ValueError):
        return 1
    if not c or not c.get("certificate") or not c.get("key"):
        return 1
    cert, key = base64.b64decode(c["certificate"]), base64.b64decode(c["key"])
    out.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, data, mode in (("cert.pem", cert, 0o644), ("key.pem", key, 0o600)):
        p = out / name
        if not p.exists() or p.read_bytes() != data:
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.chmod(tmp, mode)
            tmp.replace(p)
            changed = True
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
