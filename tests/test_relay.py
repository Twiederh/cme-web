import asyncio, json, os, sys
import tempfile
os.environ.update(RELAY_HTTP_PORT="18090", RELAY_LOCALPUSH_PORT="15070", RELAY_LOCALPUSH_SECRET="s3cret", APNS_KEY_PATH="/nonexistent", RELAY_DATA_DIR=tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "push-relay"))
import relay, httpx
from aiohttp import web

TOK = "a"*64
async def main():
    assert relay.voip_token(f"{'b'*64}:remote&{TOK}:voip") == TOK
    assert relay.voip_token(TOK.upper()) == TOK
    assert relay.voip_token("") is None
    cfg = relay.Config(); hub = relay.LocalPushHub(cfg); apns = relay.APNs(cfg)
    srv = await asyncio.start_server(hub.handle, "127.0.0.1", 15070)
    runner = web.AppRunner(relay.build_app(cfg, hub, apns)); await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18090).start()

    # Bad secret rejected
    r, w = await asyncio.open_connection("127.0.0.1", 15070)
    w.write(json.dumps({"type":"hello","token":TOK,"secret":"x"}).encode()+b"\n"); await w.drain()
    print("bad secret ->", json.loads(await r.readline())); w.close()

    # Device connects
    r, w = await asyncio.open_connection("127.0.0.1", 15070)
    w.write(json.dumps({"type":"hello","token":TOK,"secret":"s3cret"}).encode()+b"\n"); await w.drain()
    print("hello ->", json.loads(await r.readline()))
    async def device():
        while True:
            msg = json.loads(await r.readline())
            print("device got", msg)
            if msg["type"] == "call":
                w.write(json.dumps({"type":"ack","call-id":msg["call-id"]}).encode()+b"\n"); await w.drain()
    dt = asyncio.create_task(device())
    async with httpx.AsyncClient() as c:
        resp = await c.get("http://127.0.0.1:18090/push", params={"type":"apple","token":f"{TOK}:voip","call-id":"abc123","from-uri":"sip:0711@192.168.11.4","from-name":"Test"})
        print("push (connected) ->", resp.status_code, resp.json())
        dt.cancel(); w.close(); await asyncio.sleep(0.2)
        resp = await c.get("http://127.0.0.1:18090/push", params={"type":"apple","token":f"{TOK}:voip","call-id":"def"})
        print("push (not connected, no apns) ->", resp.status_code, resp.json())
        print("health ->", (await c.get("http://127.0.0.1:18090/health")).json())
    await runner.cleanup(); srv.close()
asyncio.run(main())

# VoIP-Push wie Flexisip: liblinphone sucht "call-id" und "uuid" in "aps"
pl = relay.voip_payload({"call-id": "92BB@192.168.11.4", "from-uri": "sip:0179@192.168.11.4", "display-name": "Thomas",
                         "uuid": '"<urn:uuid:5b007447>"'})
assert pl["aps"]["call-id"] == "92BB@192.168.11.4" and pl["aps"]["uuid"] == "<urn:uuid:5b007447>", pl
assert pl["aps"]["loc-key"] == "IC_MSG" and pl["aps"]["loc-args"] == ["Thomas"] and pl["from-uri"] == "sip:0179@192.168.11.4"
print("voip_payload ok")
