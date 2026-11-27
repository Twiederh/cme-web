#!/usr/bin/env python3
"""Passwörter der Apps (von cme-web, phoneapp.write_flexisip_passwd) in db_text-Tabellen für Kamailio umsetzen.

Eingabe (eine Zeile je Gerät und From-Domain, erste Zeile "version:1"):
    +4961874191283@cme.local md5:<HA1 aus Anmeldename:Realm:Passwort> ; MOBiPhone
Ausgabe im db_text-Verzeichnis (htable "ha1" und "num", Schlüssel = Anmeldename in Kleinbuchstaben):
    subs_ha1:  mobiphone -> <HA1>
    subs_num:  mobiphone -> +4961874191283

Aufruf: passwd2htable.py <passwd-datei> <db_text-verzeichnis>
Exit 0 = geschrieben (oder unverändert), 1 = Eingabe fehlt/ungültig (alte Tabellen bleiben).
"""
import re
import sys
from pathlib import Path

LINE = re.compile(r"^([^@\s]+)@\S+ md5:([0-9a-f]{32}) ; ([A-Za-z0-9._-]+)$")
HEADER = "key_name(str) key_type(int) value_type(int) key_value(str) expires(int)\n"


def main() -> int:
    src, dbdir = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        lines = src.read_text().splitlines()
    except OSError:
        return 1
    if not lines or lines[0].strip() != "version:1":
        return 1
    ha1, num = {}, {}
    for raw in lines[1:]:
        raw = raw.strip()
        if not raw:
            continue
        m = LINE.match(raw)
        if not m:
            return 1
        number, h, name = m.groups()
        ha1[name.lower()] = h
        num[name.lower()] = number
    dbdir.mkdir(parents=True, exist_ok=True)
    changed = False
    for table, data in (("subs_ha1", ha1), ("subs_num", num)):
        # db_text trennt Spalten mit ":" - Werte enthalten keins (HA1 hex, Rufnummer, Name aus [A-Za-z0-9._-])
        body = HEADER + "".join(f"{k}:0:0:{v}:0\n" for k, v in sorted(data.items()))
        path = dbdir / table
        if not path.exists() or path.read_text() != body:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body)
            tmp.replace(path)
            changed = True
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
