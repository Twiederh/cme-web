import re
import socket
import threading
import time

import sharedline

SHOW = """Shared-Line info details:

Shared-Line: '+4961874191283',  subscribed users: 2,  max calls limit: 2
Index        Users                  sub_id         peer_tag        Status
=====        =====                  ======         ========        ======
  1          +4961874191283@192.168.11.46        2            40001           IDLE
  2          +4961874191283@192.168.7.116        40           40006           IDLE
Free call queue size: 2,   Active call queue size: 0

Shared-Line: '+4961874191284',  subscribed users: 0,  max calls limit: 2
"""


def test_parse_shared_users():
    u = sharedline.parse_shared_users(SHOW)
    assert u["+4961874191283"] == {"192.168.11.46", "192.168.7.116"}
    assert u["+4961874191284"] == set()


def test_wanted_only_foreign_devices_on_shared_lines():
    dns = {"2": {"number": "+4961874191283", "shared": True}, "3": {"number": "+4961874170058", "shared": False}}
    pools = {
        "1": {"type": "", "device_id": "", "numbers": [{"ln": "1", "dn": "2"}, {"ln": "2", "dn": "3"}]},   # Gigaset
        "4": {"type": "8845", "device_id": "", "numbers": [{"ln": "1", "dn": "2"}]},                       # Cisco
        "6": {"type": "Jabber-CSF-Client", "device_id": "CSF4961874191283", "numbers": [{"ln": "1", "dn": "2"}]},
        "9": {"type": "", "device_id": "", "numbers": [{"ln": "1", "dn": "2"}]},                          # nicht angemeldet
    }
    brief = {"1": {"state": "REGISTERED", "ip": "192.168.7.213"}, "4": {"state": "REGISTERED", "ip": "192.168.11.46"},
             "6": {"state": "REGISTERED", "ip": "192.168.7.116"}, "9": {"state": "UNREGISTERED", "ip": ""}}
    assert sharedline.wanted(dns, pools, brief, {}) == [{"number": "+4961874191283", "ip": "192.168.7.213", "pool": "1"}]


def _fake_cme():
    """Antwortet 200 auf SUBSCRIBE und schickt danach ein NOTIFY; merkt sich die Antwort darauf."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    seen = {"subscribe": [], "notify_reply": []}

    def run():
        while True:
            try:
                data, addr = s.recvfrom(65535)
            except OSError:
                return
            msg = data.decode()
            if msg.startswith("SUBSCRIBE"):
                seen["subscribe"].append(msg)
                h = {k: v for k, v in re.findall(r"^(Via|From|To|Call-ID|CSeq):\s*(.*)$", msg, re.M)}
                s.sendto(("SIP/2.0 200 OK\r\n" + f"Via: {h['Via']}\r\nFrom: {h['From']}\r\nTo: {h['To']};tag=cme1\r\n"
                          f"Call-ID: {h['Call-ID']}\r\nCSeq: {h['CSeq']}\r\nContact: <sip:x@127.0.0.1:5060>\r\n"
                          "Expires: 600\r\nContent-Length: 0\r\n\r\n").encode(), addr)
                s.sendto(("NOTIFY sip:x@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKn1\r\n"
                          f"From: {h['To']};tag=cme1\r\nTo: {h['From']}\r\nCall-ID: {h['Call-ID']}\r\nCSeq: 101 NOTIFY\r\n"
                          "Event: dialog\r\nSubscription-State: active;expires=600\r\nContent-Length: 0\r\n\r\n").encode(), addr)
            elif msg.startswith("SIP/2.0 200"):
                seen["notify_reply"].append(msg)

    threading.Thread(target=run, daemon=True).start()
    return s, seen


def test_agent_subscribes_with_device_ip_and_acks_notify(monkeypatch):
    cme, seen = _fake_cme()
    port = cme.getsockname()[1]
    agent = sharedline.Agent("127.0.0.1", "127.0.0.1", 0)
    monkeypatch.setattr(agent, "_send", lambda text: agent.sock.sendto(text.encode(), ("127.0.0.1", port)))
    agent.port = agent.sock.getsockname()[1]
    want = [{"number": "+4961874191283", "ip": "192.168.7.213", "pool": "1"}]
    for _ in range(sharedline.MISSING_ROUNDS):
        agent.sync(want, {})
    st = agent.status()
    assert st and st[0]["state"] == "aktiv", st
    assert "From: <sip:+4961874191283@192.168.7.213>" in seen["subscribe"][0]
    time.sleep(0.3)
    assert seen["notify_reply"], "NOTIFY nicht bestätigt"
    # Gerät weg -> Abo wird beendet (Expires 0)
    agent.sync([], {})
    assert agent.status() == []
    assert "Expires: 0" in seen["subscribe"][-1]


def test_self_subscribed_device_is_left_alone(monkeypatch):
    agent = sharedline.Agent("127.0.0.1", "127.0.0.1", 0)
    monkeypatch.setattr(agent, "_send", lambda text: (_ for _ in ()).throw(AssertionError("darf nicht senden")))
    want = [{"number": "+49", "ip": "192.168.7.213", "pool": "1"}]
    for _ in range(3):
        agent.sync(want, {"+49": {"192.168.7.213"}})
    assert agent.status() == []


def test_app_register_checks_mobile_pool_password():
    accounts = {"MOBIphone": {"number": "+4961874191283", "password": "Geheim123"},
                "MOBIpad": {"number": "+4961874192090", "password": "Anders123"}}
    agent = sharedline.Agent("127.0.0.1", "127.0.0.1", 0, "127.0.0.1:5062", lambda n: accounts.get(n, {}), "cme.local")
    agent_port = agent.sock.getsockname()[1]
    app = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    app.bind(("127.0.0.1", 0))
    app.settimeout(2)

    def reg(auth=""):
        msg = ("REGISTER sip:cme.local SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:5062;branch=z9hG4bK1\r\n"
               "From: <sip:+4961874191283@cme.local>;tag=a\r\nTo: <sip:+4961874191283@cme.local>\r\n"
               "Call-ID: c1\r\nCSeq: 1 REGISTER\r\nContact: <sip:+4961874191283@10.0.0.5:5062;transport=tcp>\r\n"
               "Expires: 3600\r\n" + (f"Authorization: {auth}\r\n" if auth else "") + "Content-Length: 0\r\n\r\n")
        app.sendto(msg.encode(), ("127.0.0.1", agent_port))
        return app.recvfrom(65535)[0].decode()

    r = reg()
    assert r.startswith("SIP/2.0 401"), r
    ch = sharedline._header(r, "WWW-Authenticate")
    bad = sharedline.authorization(ch, "REGISTER", "sip:cme.local", "MOBIphone", "falsch")
    assert reg(bad).startswith("SIP/2.0 401")
    ch = sharedline._header(reg(), "WWW-Authenticate")
    other = sharedline.authorization(ch, "REGISTER", "sip:cme.local", "MOBIpad", "Anders123")
    assert reg(other).startswith("SIP/2.0 403"), "Gerät einer anderen Nummer"
    ch = sharedline._header(reg(), "WWW-Authenticate")
    good = sharedline.authorization(ch, "REGISTER", "sip:cme.local", "MOBIphone", "Geheim123")
    ok = reg(good)
    assert ok.startswith("SIP/2.0 200"), ok
    assert "Expires: 3600" in ok and "expires=3600" in ok


def test_app_register_unknown_device_is_forbidden():
    agent = sharedline.Agent("127.0.0.1", "127.0.0.1", 0, "127.0.0.1:5062", lambda n: {}, "cme.local")
    app = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    app.bind(("127.0.0.1", 0))
    app.settimeout(2)
    auth = 'Digest username="MOBGibtsNicht", realm="cme.local", nonce="x", uri="sip:cme.local", response="0"'
    app.sendto(("REGISTER sip:cme.local SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bK2\r\nFrom: <sip:+49@cme.local>;tag=a\r\n"
                f"To: <sip:+49@cme.local>\r\nCall-ID: c2\r\nCSeq: 1 REGISTER\r\nAuthorization: {auth}\r\n"
                "Content-Length: 0\r\n\r\n").encode(), ("127.0.0.1", agent.sock.getsockname()[1]))
    assert app.recvfrom(65535)[0].decode().startswith("SIP/2.0 403")


def test_mobile_pool_registers_at_cme_with_flexisip_contact(monkeypatch):
    cme = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cme.bind(("127.0.0.1", 0))
    seen = []

    def run():
        while True:
            data, addr = cme.recvfrom(65535)
            msg = data.decode()
            seen.append(msg)
            h = {k: v for k, v in re.findall(r"^(Via|From|To|Call-ID|CSeq):\s*(.*)$", msg, re.M)}
            base = f"Via: {h['Via']}\r\nFrom: {h['From']}\r\nTo: {h['To']};tag=x\r\nCall-ID: {h['Call-ID']}\r\nCSeq: {h['CSeq']}\r\n"
            if "Authorization:" not in msg:
                cme.sendto(("SIP/2.0 401 Unauthorized\r\n" + base +
                            'WWW-Authenticate: Digest realm="wiederhol.de",nonce="N1",algorithm=MD5,qop="auth"\r\n'
                            "Content-Length: 0\r\n\r\n").encode(), addr)
            else:
                cme.sendto(("SIP/2.0 200 OK\r\n" + base + "Content-Length: 0\r\n\r\n").encode(), addr)

    threading.Thread(target=run, daemon=True).start()
    agent = sharedline.Agent("127.0.0.1", "127.0.0.1", 0, "192.168.1.110:5062")
    monkeypatch.setattr(agent, "_send", lambda text: agent.sock.sendto(text.encode(), cme.getsockname()))
    agent.sync_regs([{"number": "+4961874191283", "device": "MOB4961874191283", "username": "MOB4961874191283",
                      "password": "Geheim123"}])
    st = agent.reg_status()
    assert st[0]["state"] == "angemeldet", st
    last = seen[-1]
    assert '<sip:+4961874191283@192.168.1.110:5062;transport=udp>' in last
    assert 'devicename.ccm.cisco.com="MOB4961874191283"' in last
    assert 'username="MOB4961874191283"' in last
