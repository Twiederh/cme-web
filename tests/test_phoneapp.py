"""Test der Softphone-Verwaltung in cme-web (phoneapp.py) zusammen mit dem push-relay. Start: CME_DEMO=true python3 tests/test_phoneapp.py"""
import os, sys, json, subprocess, tempfile, io, time
d = tempfile.mkdtemp()
os.environ.update(VM_CONFIG_DIR=d+"/cfg", PHONEAPP_DATA_DIR=d+"/push", VM_REC_DIR=d+"/rec", APNS_TEAM_ID="83BLV56527",
                  CME_HOST="192.168.11.4", VM_HOST_IP="192.168.1.110", SIP_GW_NAME="sip-gw.wiederhol.de")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import shutil
P = tempfile.gettempdir() + "/cme-web-demo-push"
shutil.rmtree(P, ignore_errors=True)
try:
    os.remove(tempfile.gettempdir() + "/cme-web-demo-vm/config/cme-phoneapp.json")
except FileNotFoundError:
    pass
import cme_web
app = cme_web.create_app(cme_web.DemoRouter())
c = app.test_client()
def check(cond, msg): print("✓" if cond else "✗", msg); assert cond, msg
pa = app.config.get("RECORDING")
check(pa and "active" in pa, "Aufzeichnung stellt active/set bereit")
j = c.get("/api/phoneapp").get_json()
check(j["ok"] and j["token"] and j["gateway_host"] == "sip-gw.wiederhol.de", f"Status {j.get('gateway_host')}")
tok = j["token"]
check(c.get("/client/v1/info?ext=21").status_code == 401, "Client ohne Schlüssel -> 401")
check(c.get("/client/v1/info?ext=21", headers={"Authorization": "Bearer falsch"}).status_code == 401, "falscher Schlüssel -> 401")
H = {"Authorization": f"Bearer {tok}"}
i = c.get("/client/v1/info?ext=21", headers=H).get_json()
print(json.dumps(i, indent=1))
check(i["ok"] and i["device_name"] == "CSF21" and i["localpush"]["secret"], "Info für die App")
check(c.get("/client/v1/info?ext=abc", headers=H).status_code == 400, "ungültige Nebenstelle")
# Authentication URL der Tischtelefone
pw = j["exec_password"]
check(j["exec_user"] == "cmephone" and len(pw) == 15 and pw.isalnum(), "Steuerungs-Zugang erzeugt")
check(i["deskphone"]["password"] == pw, "App bekommt den Steuerungs-Zugang")
check(c.get(f"/ipphone/authenticate?UserID=cmephone&Password={pw}&devicename=SEP001122334455").get_data(as_text=True) == "AUTHORIZED", "richtiges Passwort -> AUTHORIZED")
check(c.get("/ipphone/authenticate.asp?UserID=cmephone&Password=falsch").get_data(as_text=True) == "UN-AUTHORIZED", "falsches Passwort -> UN-AUTHORIZED")
# APNs-Schluessel
gen = subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"], capture_output=True, check=True).stdout
pk8 = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt"], input=gen, capture_output=True, check=True).stdout
rsa = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
rsa8 = subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt"], input=rsa, capture_output=True, check=True).stdout
def up(data, name, **kw):
    form = {"team_id": "83BLV56527", "env": "auto", **kw}
    if data is not None: form["keyfile"] = (io.BytesIO(data), name)
    return c.post("/api/phoneapp/apns", data=form, headers={"X-CME-Request": "1"}, content_type="multipart/form-data")
r = up(rsa8, "AuthKey_ABCDE12345.p8"); check(r.status_code == 400 and "P-256" in r.get_json()["error"], "RSA abgelehnt")
r = up(None, "x"); check(r.status_code == 400, "ohne Datei beim ersten Mal abgelehnt")
r = up(pk8, "AuthKey_ABCDE12345.p8"); check(r.get_json()["ok"], "Schlüssel gespeichert, Key-ID aus Dateiname")
check(oct(os.stat(P+"/apns/AuthKey.p8").st_mode)[-3:] == "600", "Datei 0600")
r = up(None, "", env="sandbox"); check(r.get_json()["ok"], "nur Umgebung geändert")
m = json.load(open(P+"/apns/apns.json")); check(m["key_id"] == "ABCDE12345" and m["env"] == "sandbox", "apns.json")
# Relay liest das
os.environ.update(RELAY_DATA_DIR=P, APNS_KEY_PATH="/nonexistent")
sys.path.insert(0, os.path.join(ROOT, "push-relay"))
import relay, jwt
cfg = relay.Config(); apns = relay.APNs(cfg)
check(apns.configured and apns.env == "sandbox", "Relay sieht den Schlüssel")
t1 = apns._token(); check(jwt.get_unverified_header(t1)["kid"] == "ABCDE12345", "JWT mit Key-ID")
time.sleep(0.02); up(pk8, "AuthKey_ZZZZZ99999.p8")
t2 = apns._token(); check(jwt.get_unverified_header(t2)["kid"] == "ZZZZZ99999", "Relay erneuert JWT nach Änderung durch cme-web")
check(cfg.secret() == i["localpush"]["secret"], "Relay nutzt das Secret aus cme-web")
old = cfg.secret(); c.post("/api/phoneapp/renew", json={"what": "secret"}); check(cfg.secret() != old, "neues Secret")
c.post("/api/phoneapp/renew", json={"what": "token"})
check(c.get("/client/v1/info?ext=21", headers=H).status_code == 401, "alter App-Schlüssel ungültig")
check(c.delete("/api/phoneapp/apns").get_json()["ok"] and not apns.configured, "Schlüssel gelöscht")
r = c.post("/api/phoneapp/settings", json={"gateway_host": "sip gw", "gateway_port": 5062}); check(r.status_code == 400, "ungültiger Host")
r = c.post("/api/phoneapp/settings", json={"gateway_host": "192.168.1.110", "gateway_port": 5062, "localpush_port": 5072, "ssids": "A, B"})
check(r.get_json()["ok"], "Einstellungen gespeichert")
# Aufzeichnung: Demo-Status
tok = c.get("/api/phoneapp").get_json()["token"]; H = {"Authorization": f"Bearer {tok}"}
st = {"time": time.time(), "active": [{"id": "20261122-160000-abcdef", "numbers": ["21", "+4971112345678"], "recording": False, "start": time.time() - 10}]}
os.makedirs(d+"/rec", exist_ok=True)
import recording
recdir = None
for p in [d+"/rec", tempfile.gettempdir() + "/cme-web-demo-rec"]:
    os.makedirs(p, exist_ok=True); json.dump(st, open(p + "/.status.json", "w"))
calls = c.get("/client/v1/calls?ext=21", headers=H).get_json()
print(calls)
check(calls["ok"] and len(calls["calls"]) == 1 and calls["calls"][0]["other"] == "+4971112345678", "laufendes Gespräch der Nebenstelle")
check(c.get("/client/v1/calls?ext=22", headers=H).get_json()["calls"] == [], "fremde Nebenstelle sieht nichts")
r = c.post("/client/v1/calls/20261122-160000-abcdef", json={"ext": "21", "on": True}, headers=H)
check(r.get_json().get("ok"), "Aufnahme einschalten")
r = c.post("/client/v1/calls/20261122-160000-abcdef", json={"ext": "22", "on": True}, headers=H)
check(r.status_code == 400, "fremde Nebenstelle darf nicht schalten")
# Mitteilungen: Push-Token eines Mobilgeräts, gehaltenes Gespräch -> Auftrag im outbox-Ordner des Relays
import glob
json.dump({"MOBIphone": {"number": "+4961874191283", "password": "x"}},
          open(tempfile.gettempdir() + "/cme-web-demo-vm/config/cme-phoneapp-mobile.json", "w"))
r = c.post("/client/v1/push", json={"device": "mobiphone", "token": "ab" * 32, "env": "prod"}, headers=H)
check(r.get_json().get("ok"), "Push-Token speichern")
check(c.post("/client/v1/push", json={"device": "Fremd", "token": "ab" * 32}, headers=H).status_code == 400,
      "unbekanntes Gerät -> 400")
app.config["PHONEAPP_MOBILE"]["notify_held"]("held", "+4961874191283", {
    "call_id": "3E11@192.168.11.4", "local_tag": "5006ab", "remote_tag": "13ED",
    "remote_uri": "sip:001796907106@192.168.11.4", "remote_name": "Thomas Handy"}, "sip:+49@192.168.11.46")
jobs = [json.load(open(f)) for p in (d + "/push", tempfile.gettempdir() + "/cme-web-demo-push")
        for f in glob.glob(p + "/outbox/*.json")]
check(len(jobs) == 1 and jobs[0]["token"] == "ab" * 32 and jobs[0]["payload"]["aps"]["category"] == "HELD_CALL"
      and jobs[0]["payload"]["held"]["localTag"] == "5006ab", "Mitteilung „gehalten“ für den Relay")

# Voicemail in der App (Demo-Postfach 101) und Gesprächsaufnahmen der Nebenstelle
vmj = c.get("/client/v1/voicemail?ext=101", headers=H).get_json()
check(vmj["ok"] and vmj["box"] == "101" and len(vmj["messages"]) == 2 and vmj["messages"][0]["new"], "Voicemail-Liste der App")
m0 = vmj["messages"][0]
r = c.get(f"/client/v1/voicemail/101/{m0['folder']}/{m0['file']}/audio?ext=101", headers=H)
check(r.status_code == 200 and r.data[:4] == b"RIFF", "Voicemail abspielen (WAV)")
check(c.get(f"/client/v1/voicemail/101/{m0['folder']}/{m0['file']}/audio?ext=21", headers=H).status_code == 400,
      "fremde Nebenstelle: kein Zugriff aufs Postfach")
check(c.get("/client/v1/voicemail?ext=21", headers=H).get_json()["messages"] == [], "Nebenstelle ohne Postfach: leer")
for p in [d+"/rec", tempfile.gettempdir() + "/cme-web-demo-rec"]:
    json.dump({"start": time.time() - 60, "duration": 12, "direction": "in", "internal": "21", "external": "+4971112345678",
               "file": "20261122-150000-abc123.mp3"}, open(p + "/20261122-150000-abc123.json", "w"))
    open(p + "/20261122-150000-abc123.mp3", "wb").write(b"ID3fake")
rl = c.get("/client/v1/recordings?ext=21", headers=H).get_json()
check(rl["ok"] and [x["id"] for x in rl["recordings"]] == ["20261122-150000-abc123"], "Aufnahmen der Nebenstelle")
check(c.get("/client/v1/recordings?ext=22", headers=H).get_json()["recordings"] == [], "fremde Nebenstelle: keine Aufnahmen")
r = c.get("/client/v1/recordings/20261122-150000-abc123/audio?ext=21", headers=H)
check(r.status_code == 200 and r.data == b"ID3fake" and r.mimetype == "audio/mpeg", "Aufnahme abspielen")
check(c.delete("/client/v1/recordings/20261122-150000-abc123?ext=22", headers=H).status_code == 400, "fremde Nebenstelle darf nicht löschen")
check(c.delete("/client/v1/recordings/20261122-150000-abc123?ext=21", headers=H).get_json()["ok"]
      and c.get("/client/v1/recordings?ext=21", headers=H).get_json()["recordings"] == [], "Aufnahme gelöscht")

# Passwortdatei für Flexisip (Digest der App-Anfragen): je Mobilgerät eine Zeile pro From-Domain
os.environ.update(SIP_DOMAIN="cme.local", CME_HOST="192.168.11.4", VM_HOST_IP="192.168.1.110")
r = c.post("/api/phoneapp/mobiles/MOBIphone/password", json={})
check(r.get_json().get("ok"), "neues Passwort für MOBIphone")
pw = json.load(open(tempfile.gettempdir() + "/cme-web-demo-vm/config/cme-phoneapp-mobile.json"))["MOBIphone"]["password"]
pf = [open(p + "/flexisip-passwd").read() for p in (d + "/push", P) if os.path.exists(p + "/flexisip-passwd")]
check(len(pf) == 1, "flexisip-passwd geschrieben")
lines = pf[0].splitlines()
import hashlib
ha1 = hashlib.md5(f"MOBIphone:cme.local:{pw}".encode()).hexdigest()    # HA1 mit dem Anmeldenamen der App
check(lines[0] == "version:1" and f"+4961874191283@cme.local md5:{ha1} ; MOBIphone" in lines
      and f"+4961874191283@192.168.11.4 md5:{ha1} ; MOBIphone" in lines
      and f"+4961874191283@192.168.1.110 md5:{ha1} ; MOBIphone" in lines, "Zeilen für SIP-Domain, CME-IP, Flexisip-IP")
check(pw not in pf[0], "kein Passwort im Klartext")
print("ALLE PHONEAPP-TESTS OK")
