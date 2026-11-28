"""Test der Jitsi-Anbindung (jitsi.py, kamailio/passwd2htable.py --conf). Start: python3 tests/test_jitsi.py"""
import os, sys, tempfile, subprocess
from pathlib import Path
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import jitsi as J


def check(cond, msg):
    print("✓" if cond else "✗", msg)
    assert cond, msg


A = "net.java.sip.communicator.impl.protocol.sip.acc1403273890647"
PROPS = f"""# Kommentar
org.jitsi.jigasi.DEFAULT_JVB_ROOM_NAME=siptest@conference.jitsi.example.de
org.jitsi.jigasi.MUC_SERVICE_ADDRESS=conference.jitsi.example.de
org.jitsi.jigasi.SIP_PORT=5060
{A}=acc1403273890647
{A}.ACCOUNT_UID=SIP\\:jitsi
{A}.USER_ID=jitsi
{A}.PROXY_ADDRESS=192.168.2.4
{A}.PROXY_PORT=5060
{A}.IS_PROXY_OVERRIDDEN=true
{A}.PREFERRED_TRANSPORT=UDP
{A}.Encodings.G722/8000=700
{A}.Encodings.PCMA/8000=600
{A}.Encodings.PCMU/8000=650
{A}.Encodings.opus/48000=1000
#org.jitsi.jigasi.xmpp.acc.USER_ID=SOME_USER@SOME_DOMAIN
net.java.sip.communicator.impl.protocol.jabber.acc-xmpp-1.USER_ID=jigasi@auth.jitsi.example.de
net.java.sip.communicator.impl.protocol.jabber.acc-xmpp-1.PASSWORD=geheim
net.java.sip.communicator.impl.protocol.jabber.acc-xmpp-1.DOMAIN_BASE=jitsi.example.de
#base64 AES keyLength:256 or 128
net.java.sip.communicator.impl.protocol.jabber.acc-xmpp-1.PASSWORD=
"""
p = J.parse_props(PROPS)
m = J.model(p, "192.168.1.110", 5068)
check(m["account"] == "acc1403273890647" and m["proxy"] == "192.168.2.4" and not m["via_kamailio"], "SIP-Konto erkannt")
check(m["domain"] == "jitsi.example.de" and m["muc"] == "conference.jitsi.example.de" and m["sip_port"] == 5060, "Domain, MUC, Port")
check(m["encodings"]["opus/48000"] == 1000 and not m["xmpp_login"], "Codecs, anonym")

ch = J.changes_for(p, {"kamailio": True}, "192.168.1.110", 5068)
new = J.set_props(PROPS, ch)
m2 = J.model(J.parse_props(new), "192.168.1.110", 5068)
check(m2["via_kamailio"] and m2["proxy_port"] == "5068", "Mit Kamailio verbinden")
check("#org.jitsi.jigasi.xmpp.acc.USER_ID=SOME_USER" in new and "ACCOUNT_UID=SIP\\:jitsi" in new, "Kommentare und Escapes bleiben")
check(new.count(f"{A}.PROXY_ADDRESS=") == 1, "Wert an seiner Stelle geändert, nicht doppelt")

ch = J.changes_for(p, {"xmpp_login": True, "default_room": "team", "trust_all": False,
                       "encodings": {"opus/48000": 0, "G722/8000": 900, "PCMA/8000": 800, "PCMU/8000": 0}}, "192.168.1.110", 5068)
new = J.set_props(PROPS, ch)
q = J.parse_props(new)
check(q["org.jitsi.jigasi.xmpp.acc.USER_ID"] == "jigasi@auth.jitsi.example.de" and q["org.jitsi.jigasi.xmpp.acc.PASS"] == "geheim"
      and q["org.jitsi.jigasi.xmpp.acc.ANONYMOUS_AUTH"] == "false", "Anmeldung mit dem Jigasi-Konto")
check(q[J.K_ROOM] == "team@conference.jitsi.example.de" and q[f"{A}.Encodings.opus/48000"] == "0", "Standard-Raum, Codecs")
check(J.K_TRUST not in q, "Zertifikatsprüfung bleibt an")
for bad in ({"encodings": {c: 0 for c in J.CODECS}}, {"proxy": "a b"}, {"default_room": "Raum mit Leerzeichen"}, {"user_id": "a;b"}):
    try:
        J.changes_for(p, bad, "192.168.1.110", 5068)
        check(False, f"abgelehnt: {bad}")
    except J.JitsiError:
        check(True, f"abgelehnt: {list(bad)[0]}")

rooms = J.check_rooms([{"number": "8888", "room": "siptest", "name": ""}, {"number": "8801", "room": "Team", "name": "team"},
                       {"number": "", "room": "", "name": ""}])
check(len(rooms) == 2 and rooms[1]["room"] == "team", "Räume geprüft")
try:
    J.check_rooms([{"number": "8888", "room": "a"}, {"number": "8888", "room": "b"}])
    check(False, "doppelte Nummer")
except J.JitsiError:
    check(True, "doppelte Nummer abgelehnt")

conf = J.conf_file("192.168.2.111", 5060, rooms, "conference.jitsi.example.de")
check("@ip 192.168.2.111" in conf and "8801 team@conference.jitsi.example.de" in conf and "=team team@" in conf, "Datei für Kamailio")
d = Path(tempfile.mkdtemp())
(d / "conferences").write_text(conf + "kaputt:zeile x\n")
out = subprocess.run([sys.executable, f"{ROOT}/kamailio/passwd2htable.py", "--conf", str(d / "conferences"), str(d / "db")],
                     capture_output=True, text=True)
tab = (d / "db" / "conf_room").read_text()
check(out.returncode == 0 and out.stdout.strip() == "changed", "passwd2htable --conf")
check("8888:0:0:siptest@conference.jitsi.example.de:0" in tab and "=team:0:0:team@" in tab and "@port:0:0:5060:0" in tab
      and "kaputt" not in tab, "Tabelle conf_room")

CFG = """dial-peer voice 1 voip
 description sipgate
 destination-pattern 0T
 session target ipv4:217.10.68.150
dial-peer voice 8888 voip
 description Jigasi Trunk
 destination-pattern 8888
 session protocol sipv2
 session target ipv4:192.168.2.111:5060
 voice-class codec 3
 voice-class sip bind control source-interface GigabitEthernet0/0/0
 voice-class sip bind media source-interface GigabitEthernet0/0/0
dial-peer voice 8800 voip
 description Jitsi-Konferenz alt
 destination-pattern 8700
 session target ipv4:192.168.1.110:5068
"""
lines = J.cme_lines(rooms, CFG, "192.168.1.110", 5068, "192.168.2.111", True)
txt = "\n".join(lines)
print(txt)
check("dial-peer voice 8888 voip" in lines and " session target ipv4:192.168.1.110:5068" in lines, "alter Dial-Peer 8888 übernommen")
check(" no voice-class sip bind control source-interface GigabitEthernet0/0/0" in lines, "Bindung an die Schnittstelle entfernt")
check("no dial-peer voice 8800 voip" in lines, "Raum ohne Nummer mehr: Dial-Peer weg")
check("dial-peer voice 8801 voip" in lines and " destination-pattern 8801" in lines, "neuer Dial-Peer mit freiem Tag")
check(" pattern 192\\.168\\.1\\.110:5068" in lines and " incoming uri via JITSI" in lines, "Dial-Peer für Jigasi -> CME")
check("dial-peer voice 1 voip" not in lines, "fremde Dial-Peers bleiben")
CFG2 = CFG + "dial-peer voice 8890 voip\n description " + J.OUT_DESC + "\n incoming uri via JITSI\n"
lines = J.cme_lines([], CFG2, "192.168.1.110", 5068, "192.168.2.111", False)
check("no dial-peer voice 8890 voip" in lines and "no voice class uri JITSI" in lines and "no dial-peer voice 8888 voip" in lines,
      "alles abbauen")
hook = J.prosody_hook("/etc/letsencrypt/live/jitsi.example.de", ["jitsi.example.de", "auth.jitsi.example.de"])
check("/etc/prosody/certs/auth.jitsi.example.de.key" in hook and "systemctl restart prosody" in hook, "Renewal-Hook")
r = subprocess.run(["sh", "-n"], input=hook, text=True, capture_output=True)
check(r.returncode == 0, f"Hook ist gültiges sh {r.stderr}")
print("alles ok")
