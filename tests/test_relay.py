"""Relay hardening tests (in-process server, white-box where useful)."""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from common import ROOT, TOKEN, check, req

sys.path.insert(0, os.path.join(ROOT, "relay"))
import relay  # noqa: E402


def raw(port, data: bytes, timeout=3) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        out = b""
        try:
            while chunk := s.recv(4096):
                out += chunk
        except OSError:
            pass
        return out


def open_stream(base, dev, out):
    def run():
        r = urllib.request.Request(f"{base}/v1/stream?device={dev}", headers={"Authorization": "Bearer " + TOKEN})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                et = ""
                for line in resp:
                    line = line.decode().rstrip("\n")
                    if line.startswith("event:"):
                        et = line[6:].strip()
                    elif line.startswith("data:") and et == "speak":
                        out.append(json.loads(line[5:]))
        except OSError:
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def wait_for(cond, secs=4.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def run():
    cfg = {"devices": {"a": {}, "b": {}}, "groups": {}, "defaults": {"route": "all"}}
    st = relay.State(cfg)
    for _ in range(1500):
        st.speak({"text": "x", "target": "a"})
    check("by_id bounded to MAX_TRACKED", len(st.by_id) <= relay.MAX_TRACKED and len(st.by_id) > 900)
    st = relay.State(cfg)
    r = st.speak({"text": "x", "target": "a", "ttl_s": 99999, "session_id": {"k": 1}, "priority": 5, "play_at": 7})
    ev = st.queues["a"].get_nowait()
    check("ttl clamped, fields coerced to str", ev["ttl_s"] == 3600 and ev["session_id"] == str({"k": 1})
          and ev["priority"] == "5" and ev["play_at"] == "7")
    check("ttl_s floor is 1", st.speak({"text": "x", "target": "b", "ttl_s": -5}) and st.queues["b"].get_nowait()["ttl_s"] == 1)

    st = relay.State(cfg)
    relay.KEEPALIVE_S = 1
    H = relay.make_handler(st, [TOKEN], False)
    check("handler has socket timeout", H.timeout == 30)
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    auth = f"Authorization: Bearer {TOKEN}\r\n"
    try:
        for bad in ("-1", "abc"):
            out = raw(port, f"POST /v1/speak HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: {bad}\r\n\r\n".encode())
            check(f"Content-Length {bad} -> 400 (no hang)", out.startswith(b"HTTP/1.0 400") or out.startswith(b"HTTP/1.1 400"))
        check("dict session_id -> 202", req(base, "POST", "/v1/speak", {"text": "hi", "target": "a", "session_id": {"x": 1}})[0] == 202)
        check("non-numeric ttl_s -> 400", req(base, "POST", "/v1/speak", {"text": "hi", "target": "a", "ttl_s": "soon"})[0] == 400)
        check("ttl_s null/inf -> no 500", req(base, "POST", "/v1/speak", {"text": "hi", "target": "a", "ttl_s": 1e999})[0] in (202, 400))

        orig = relay.State.speak
        relay.State.speak = lambda self, body: (_ for _ in ()).throw(RuntimeError("secret-internals-xyz"))
        try:
            data = urllib.request.Request(base + "/v1/speak", data=b'{"text":"x"}', method="POST",
                                          headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
            try:
                urllib.request.urlopen(data, timeout=5)
                code, body = 200, b""
            except urllib.error.HTTPError as e:
                code, body = e.code, e.read()
        finally:
            relay.State.speak = orig
        check("unexpected exception -> generic 500", code == 500 and json.loads(body) == {"error": "internal error"}
              and b"secret" not in body)

        # newest connection per device wins
        g1, g2 = [], []
        t1 = open_stream(base, "b", g1)
        check("first stream connected", wait_for(lambda: st.connections["b"] == 1))
        t2 = open_stream(base, "b", g2)
        check("old stream closed, one connection left", wait_for(lambda: not t1.is_alive() and st.connections["b"] == 1))
        snap = req(base, "GET", "/v1/state")[1]
        check("state shows device connected", snap["devices"]["b"]["connected"] is True)
        req(base, "POST", "/v1/speak", {"text": "nur neu", "target": "b"})
        check("event goes to newest stream only", wait_for(lambda: len(g2) == 1) and g1 == [])
        # drain: client closes -> keepalive write fails -> counter returns to 0
        st.gen["b"] += 1   # superseded without a new client: handler must exit by itself
        check("superseded handler exits", wait_for(lambda: st.connections["b"] == 0 and not t2.is_alive(), 4))
        sk = socket.create_connection(("127.0.0.1", port), timeout=3)
        sk.sendall(f"GET /v1/stream?device=a HTTP/1.1\r\nHost: x\r\n{auth}\r\n".encode())
        check("raw stream connected", wait_for(lambda: st.connections["a"] == 1))
        sk.close()
        check("client disconnect -> connections 0", wait_for(lambda: st.connections["a"] == 0, 5))
    finally:
        srv.shutdown()
        relay.KEEPALIVE_S = 15
