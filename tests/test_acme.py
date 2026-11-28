"""Test des Zertifikatsmoduls (acme.py) mit einer lego-Attrappe. Start: python3 tests/test_acme.py"""
import datetime, json, os, sys, tempfile, time
from pathlib import Path
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import acme as A


def check(cond, msg):
    print("✓" if cond else "✗", msg)
    assert cond, msg


check(A.parse_env("# x\nALL_INKL_LOGIN=w123\nALL_INKL_PASSWORD = a=b \n") == {"ALL_INKL_LOGIN": "w123", "ALL_INKL_PASSWORD": "a=b"},
      "Zugangsdaten gelesen (= im Wert erlaubt)")
for bad in ("kaputt", "lower=1", "A B=1"):
    try:
        A.parse_env(bad)
        check(False, f"abgelehnt: {bad}")
    except A.AcmeError:
        check(True, f"abgelehnt: {bad}")
check(A.merge_env({"X_A": A.MASK, "X_B": "neu"}, {"X_A": "alt", "X_B": "alt"}) == {"X_A": "alt", "X_B": "neu"}, "ausgeblendete Werte bleiben")
check(A.check_domains("Sip-GW.example.de, *.example.net example.net, sip-gw.example.de.") == ["sip-gw.example.de", "*.example.net", "example.net"],
      "Domains geprüft")
try:
    A.check_domains("a;b")
    check(False, "Domain mit ;")
except A.AcmeError:
    check(True, "Domain mit ; abgelehnt")
a = A.lego_args("/d", "x@example.de", "allinkl", ["a.example.de", "b.example.de"], "1.1.1.1:53", True, False)
check(a[-1] == "run" and a.count("--domains") == 2 and "--dns.resolvers" in a and any("staging" in x for x in a), "Aufruf run")
check(A.lego_args("/d", "x@example.de", "allinkl", ["a.example.de"], "", False, True)[-4:] == ["renew", "--days", "30", "--no-random-sleep"],
      "Aufruf renew")

# Ablauf mit Attrappe: lego schreibt Zertifikat und Schlüssel, cme-web legt sie für Kamailio ab
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
d = Path(tempfile.mkdtemp())
k = ec.generate_private_key(ec.SECP256R1())
n = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "sip-gw.example.de")])
now = datetime.datetime.now(datetime.timezone.utc)
c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key()).serial_number(1)
     .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=90))
     .add_extension(x509.SubjectAlternativeName([x509.DNSName("sip-gw.example.de"), x509.DNSName("example.net")]), False)
     .sign(k, hashes.SHA256()))
(d / "src.crt").write_bytes(c.public_bytes(serialization.Encoding.PEM))
(d / "src.key").write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
lego = d / "lego"
lego.write_text(f"""#!/bin/sh
# Attrappe: Pfad aus --path, Geheimnis im Protokoll
while [ $# -gt 0 ]; do [ "$1" = --path ] && P=$2; shift; done
echo "login $ALL_INKL_PASSWORD"
mkdir -p "$P/certificates" && cp {d}/src.crt "$P/certificates/sip-gw.example.de.crt" && cp {d}/src.key "$P/certificates/sip-gw.example.de.key"
""")
os.chmod(lego, 0o755)
A.LEGO = str(lego)
import flask
app = flask.Flask()
cfg, data = d / "cfg", d / "data"
cfg.mkdir(); data.mkdir()
A.register(app, True, cfg, data)
r = flask.request
r._json = {"domains": "sip-gw.example.de, example.net", "provider": "allinkl", "email": "x@example.de",
           "env": "ALL_INKL_LOGIN=w123\nALL_INKL_PASSWORD=geheim99", "resolver": "1.1.1.1:53"}
check(app.routes[("POST", "/api/acme")]()["ok"], "gespeichert")
check(oct((cfg / "cme-acme.json").stat().st_mode)[-3:] == "600", "Einstellungen nur für cme-web lesbar")
st = app.routes[("GET", "/api/acme")]()
check("geheim99" not in json.dumps(st) and "ALL_INKL_PASSWORD=" + A.MASK in st["settings"]["env"], "Passwort nicht in der Oberfläche")
r._json = {**r._json, "env": "ALL_INKL_LOGIN=w123\nALL_INKL_PASSWORD=" + A.MASK}
app.routes[("POST", "/api/acme")]()
check(json.loads((cfg / "cme-acme.json").read_text())["env"]["ALL_INKL_PASSWORD"] == "geheim99", "Passwort bleibt beim Speichern")
app.routes[("POST", "/api/acme/issue")]()
for _ in range(50):
    st = app.routes[("GET", "/api/acme")]()
    if not st["state"]["running"]:
        break
    time.sleep(0.1)
check(st["state"]["ok"] is True, f"ausgestellt {st['state']}")
check("geheim99" not in st["state"]["log"] and A.MASK in st["state"]["log"], "Passwort nicht im Protokoll")
check((data / "tls" / "cert.pem").read_bytes() == (d / "src.crt").read_bytes()
      and oct((data / "tls" / "key.pem").stat().st_mode)[-3:] == "600", "für Kamailio abgelegt")
check(st["cert"]["names"] == ["sip-gw.example.de", "example.net"] and 88 <= st["cert"]["days"] <= 90, "Zertifikat erkannt")
print("alles ok")
