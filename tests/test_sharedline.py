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
