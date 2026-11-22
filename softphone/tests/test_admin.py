"""Test der Verwaltungsoberfläche des push-relay (APNs-Schlüssel hochladen)."""
import asyncio, base64, os, re, subprocess, sys, tempfile
d = tempfile.mkdtemp()
os.environ.update(RELAY_DATA_DIR=d, RELAY_ADMIN_PASSWORD="geheim", APNS_KEY_PATH="/nonexistent")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server", "push-relay"))
import relay, jwt
from aiohttp import FormData
from aiohttp.test_utils import TestServer, TestClient

def keyfile(name, args):
    path = os.path.join(d, name)
    gen = subprocess.run(["openssl", *args], check=True, capture_output=True).stdout
    pk8 = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt"], input=gen, check=True, capture_output=True).stdout
    open(path, "wb").write(pk8)
    return path

EC = keyfile("AuthKey_ABCDE12345.p8", ["ecparam", "-name", "prime256v1", "-genkey", "-noout"])
RSA = keyfile("rsa.p8", ["genrsa", "2048"])

def basic(u, p): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
def check(c, m): print("✓" if c else "✗", m); assert c, m

async def main():
    cfg = relay.Config(); hub = relay.LocalPushHub(cfg); apns = relay.APNs(cfg)
    async with TestClient(TestServer(relay.build_admin_app(cfg, hub, apns))) as c:
        check((await c.get("/")).status == 401, "ohne Login -> 401")
        check((await c.get("/", headers=basic("admin", "falsch"))).status == 401, "falsches Passwort -> 401")
        auth = basic("admin", "geheim")
        r = await c.get("/", headers=auth); html = await r.text()
        check(r.status == 200 and "nicht eingerichtet" in html, "Seite zeigt 'nicht eingerichtet'")
        csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        def form(path, csrf_=csrf, kid="ABCDE12345", tid="83BLV56527", env="auto"):
            f = FormData(); f.add_field("csrf", csrf_); f.add_field("key_id", kid); f.add_field("team_id", tid); f.add_field("env", env)
            if path: f.add_field("keyfile", open(path, "rb"), filename=os.path.basename(path))
            return f
        r = await c.post("/apns", data=form(EC, csrf_="x"), headers=auth)
        check("abgelaufen" in await r.text() and not apns.configured, "falsches CSRF-Token abgelehnt")
        r = await c.post("/apns", data=form(RSA), headers=auth)
        check("EC P-256" in await r.text() and not apns.configured, "RSA-Schlüssel abgelehnt")
        r = await c.post("/apns", data=form(EC, kid="kurz"), headers=auth)
        check("10 Zeichen" in await r.text(), "ungültige Key-ID abgelehnt")
        r = await c.post("/apns", data=form(None), headers=auth)
        check("auswählen" in await r.text(), "ohne Datei beim ersten Mal abgelehnt")
        r = await c.post("/apns", data=form(EC, kid="abcde12345"), headers=auth)
        check("Gespeichert" in await r.text() and apns.configured, "gültiger Schlüssel gespeichert")
        tok = apns._token(); h = jwt.get_unverified_header(tok); p = jwt.decode(tok, options={"verify_signature": False})
        check(h["kid"] == "ABCDE12345" and p["iss"] == "83BLV56527" and h["alg"] == "ES256", "JWT mit Key-ID/Team-ID")
        check(oct(os.stat(os.path.join(d, "apns", "AuthKey.p8")).st_mode)[-3:] == "600", "Schlüsseldatei 0600")
        r = await c.post("/apns", data=form(None, env="sandbox"), headers=auth)
        check("Gespeichert" in await r.text() and apns.env == "sandbox", "nur Umgebung geändert, Schlüssel bleibt")
        js = await (await c.get("/api/status", headers=auth)).json()
        check(js["source"] == "upload" and js["key_id"] == "ABCDE12345" and "PRIVATE" not in str(js), "Status-API ohne Schlüsselinhalt")
        r = await c.post("/delete", data={"csrf": csrf}, headers=auth)
        check("gelöscht" in await r.text() and not apns.configured, "Löschen")
    print("ALLE ADMIN-TESTS OK")

asyncio.run(main())
