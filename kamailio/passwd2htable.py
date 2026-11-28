#!/usr/bin/env python3
"""Passwörter der Apps (von cme-web, phoneapp.write_flexisip_passwd) in db_text-Tabellen für Kamailio umsetzen.

Eingabe (eine Zeile je Gerät und From-Domain, erste Zeile "version:1"):
    +4961874191283@cme.local md5:<HA1 aus Anmeldename:Realm:Passwort> ; MOBiPhone
Ausgabe im db_text-Verzeichnis (htable "ha1" und "num", Schlüssel = Anmeldename in Kleinbuchstaben):
    subs_ha1:  mobiphone -> <HA1>
    subs_num:  mobiphone -> +4961874191283

Aufruf: passwd2htable.py <passwd-datei> <db_text-verzeichnis>
Exit 0 = geschrieben (oder unverändert), 1 = Eingabe fehlt/ungültig (alte Tabellen bleiben).

Mit --aliases <datei> <db_text-verzeichnis>: Adressen für Anrufe von draußen (cme-web, phoneapp.write_sip_aliases)
    version:1
    <name> <rufnummer>
    @d <domain>               weitere Domain (Schlüssel "@d=<domain>"): name@<domain> wie name@SIP_PUBLIC_DOMAIN
in die Tabelle sip_alias (htable "alias", Schlüssel = Name in Kleinbuchstaben).

Mit --conf <datei> <db_text-verzeichnis>: Konferenzräume in Jitsi (cme-web, jitsi.write_conf)
    version:1
    @ip 192.168.2.111          Jigasi (SIP)
    @port 5060
    8888 raum@conference.jitsi.example.de      Rufnummer an der CME -> Raum
    =team team@conference.jitsi.example.de     Name von draußen (<name>@SIP_PUBLIC_DOMAIN) -> Raum
in die Tabelle conf_room (htable "conf").
"""
import re
import sys
from pathlib import Path

LINE = re.compile(r"^([^@\s]+)@\S+ md5:([0-9a-f]{32}) ; ([A-Za-z0-9._-]+)$")
HEADER = "key_name(str) key_type(int) value_type(int) key_value(str) expires(int)\n"


ALIAS = re.compile(r"^([a-z0-9._+-]{1,64}) (\+?[0-9]{2,32})$")
ALIAS_DOM = re.compile(r"^@d ([a-z0-9.-]{4,200})$")   # weitere Domain: name@<domain> wie name@SIP_PUBLIC_DOMAIN


def write_tables(dbdir: Path, tables) -> bool:
    dbdir.mkdir(parents=True, exist_ok=True)
    changed = False
    for table, data in tables:
        # db_text trennt Spalten mit ":" - Werte enthalten keins (HA1 hex, Rufnummer, Name aus [A-Za-z0-9._+-])
        body = HEADER + "".join(f"{k}:0:0:{v}:0\n" for k, v in sorted(data.items()))
        path = dbdir / table
        if not path.exists() or path.read_text() != body:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(body)
            tmp.replace(path)
            changed = True
    return changed


def aliases(src: Path, dbdir: Path) -> int:
    try:
        lines = src.read_text().splitlines()
    except OSError:
        lines = ["version:1"]
    if not lines or lines[0].strip() != "version:1":
        return 1
    data = {}
    for raw in lines[1:]:
        m = ALIAS.match(raw.strip().lower())
        if m:
            data[m.group(1)] = m.group(2)
        m = ALIAS_DOM.match(raw.strip().lower())
        if m:
            data["@d=" + m.group(1)] = "1"
    print("changed" if write_tables(dbdir, [("sip_alias", data)]) else "unchanged")
    return 0


CONF = re.compile(r"^(@ip|@port|[0-9*#]{1,32}|=[a-z0-9._+-]{1,64}) ([A-Za-z0-9._@-]{1,200})$")


def conf(src: Path, dbdir: Path) -> int:
    try:
        lines = src.read_text().splitlines()
    except OSError:
        lines = ["version:1"]
    if not lines or lines[0].strip() != "version:1":
        return 1
    data = {}
    for raw in lines[1:]:
        m = CONF.match(raw.strip())
        if m:
            data[m.group(1).lower()] = m.group(2)
    print("changed" if write_tables(dbdir, [("conf_room", data)]) else "unchanged")
    return 0


def main() -> int:
    if sys.argv[1] == "--conf":
        return conf(Path(sys.argv[2]), Path(sys.argv[3]))
    if sys.argv[1] == "--aliases":
        return aliases(Path(sys.argv[2]), Path(sys.argv[3]))
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
    changed = write_tables(dbdir, [("subs_ha1", ha1), ("subs_num", num)])
    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
