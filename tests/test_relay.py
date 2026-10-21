"""Relay hardening tests (in-process server, white-box where useful)."""
import json
import os
import subprocess
import tempfile
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


def run_base_path():
    check("base_path normalized", [relay.norm_base_path(x) for x in ("speak/", "/a/b", "", None, "/")] == ["/speak", "/a/b", "", "", ""])
    st = relay.State({"devices": {"a": {}, "b": {}}, "groups": {}, "defaults": {"route": "all"}})
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(st, [TOKEN], False, "/speak"))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    pre = base + "/speak"
    try:
        check("prefixed /healthz 200", req(pre, "GET", "/healthz", token=None)[0] == 200)
        check("prefixed /v1/state 401 without token", req(pre, "GET", "/v1/state", token=None)[0] == 401)
        check("prefixed /v1/state 200 with token", req(pre, "GET", "/v1/state")[0] == 200)
        check("un-prefixed /v1/state still 200", req(base, "GET", "/v1/state")[0] == 200)
        check("un-prefixed /healthz still 200", req(base, "GET", "/healthz", token=None)[0] == 200)
        check("/speakx/ and /speakers/ do not match", all(
            req(base + pfx, "GET", "/v1/state")[0] == 404 and req(base + pfx, "GET", "/v1/state", token=None)[0] in (401, 404)
            for pfx in ("/speakx", "/speakers")))
        got = []
        open_stream(pre, "a", got)
        check("SSE via prefix keeps query string", wait_for(lambda: st.connections["a"] == 1))
        tmp = tempfile.mkdtemp()
        env = {**os.environ, "CLAUDE_VOICE_URL": pre, "CLAUDE_VOICE_TOKEN": TOKEN, "CLAUDE_VOICE_TARGET": "a",
               "CLAUDE_VOICE_SYNC": "1", "CLAUDE_VOICE_STATE_DIR": tmp}
        subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=env, timeout=10,
                       input=json.dumps({"hook_event_name": "Notification", "session_id": "bp", "message": "Hallo Prefix."}).encode())
        check("hook posting to base+prefix delivers event", wait_for(lambda: len(got) == 1 and got[0]["text"] == "Hallo Prefix."))
    finally:
        srv.shutdown()


def serve(st, **kw):
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.make_handler(st, [TOKEN], False, **kw))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def run_hardening():
    import contextlib
    import io
    relay.KEEPALIVE_S = 1
    cfg = {"devices": {"a": {}, "b": {}}, "groups": {}, "defaults": {"route": "active"}}
    st = relay.State(cfg)
    srv, port = serve(st, max_streams=2)
    base = f"http://127.0.0.1:{port}"
    auth = f"Authorization: Bearer {TOKEN}\r\n"
    try:
        # R1 activity = connect/delivery/ack, not idle loop
        ga, gb = [], []
        open_stream(base, "a", ga)
        wait_for(lambda: st.connections["a"] == 1)
        time.sleep(0.3)
        open_stream(base, "b", gb)
        wait_for(lambda: st.connections["b"] == 1)
        time.sleep(3.2)
        res = req(base, "POST", "/v1/speak", {"text": "wer?", "ttl_s": 30})[1]
        check("later-connected device stays 'active' over idle", res["events"][0]["device"] == "b")
        check("event carries expires_in_s", wait_for(lambda: len(gb) == 1) and 0 < gb[0]["expires_in_s"] <= 30)
        # R8 stream cap (a, b hold both slots)
        sk = socket.create_connection(("127.0.0.1", port), timeout=3)
        sk.sendall(f"GET /v1/stream?device=a HTTP/1.1\r\nHost: x\r\n{auth}\r\n".encode())
        check("stream cap -> 503", b" 503 " in sk.recv(200))
        sk.close()
    finally:
        srv.shutdown()

    st = relay.State({"devices": {"a": {}}, "groups": {}, "defaults": {"route": "a"}})
    srv, port = serve(st)
    base = f"http://127.0.0.1:{port}"
    try:
        # R2 full queue: requeue never blocks, event dropped
        for _ in range(100):
            st.speak({"text": "x"})
        ev = {"id": "evt_zz", "_expires": time.time() + 99}
        st.by_id["evt_zz"] = {"id": "evt_zz", "device": "a", "status": "queued", "ts": 0}
        t = threading.Thread(target=st.requeue, args=("a", ev), daemon=True)
        t.start()
        t.join(3)
        check("requeue into full queue does not block, marks dropped", not t.is_alive() and st.by_id["evt_zz"]["status"] == "dropped:queue_full")
        # R3 purge expired before enqueue
        st = relay.State({"devices": {"a": {}}, "groups": {}, "defaults": {"route": "a"}})
        for _ in range(100):
            st.speak({"text": "x", "ttl_s": 1})
        time.sleep(1.2)
        r = st.speak({"text": "neu"})
        old = [x for x in st.history if x["status"] == "expired"]
        check("expired events purged, new event queued", r["events"][0]["status"] == "queued" and len(old) == 100)
        # R4 caps
        check("overlong session_id -> 400", req(base, "POST", "/v1/speak", {"text": "x", "session_id": "s" * 129})[0] == 400)
        check("unknown ack status -> 400", req(base, "POST", "/v1/ack", {"id": "x", "device": "a", "status": "evil"})[0] == 400)
        code, _ = req(base, "POST", "/v1/speak", {"text": "x", "target": "geheim-ziel"})
        pl = b'{"text":"x","target":"geheimzz"}'
        raw_resp = raw(port, f"POST /v1/speak HTTP/1.1\r\nHost: x\r\n{auth}Content-Length: {len(pl)}\r\n\r\n".encode() + pl)
        check("404 does not echo target", code == 404 and b"geheimzz" not in raw_resp and b" 404 " in raw_resp)
        st2 = relay.State({"devices": {"a": {}}, "groups": {}})
        for i in range(1100):
            st2.set_mute("session", f"s{i}", True)
        check("muted sessions capped, oldest evicted", len(st2.muted["session"]) == 1000 and "s0" not in st2.muted["session"]
              and "s1099" in st2.muted["session"])
        # R5 path handling
        for line in ("GET //x/v1/state HTTP/1.1", "GET /v1/state;x HTTP/1.1", "GET //[ HTTP/1.1", "GET /v1/state%00 HTTP/1.1"):
            out = raw(port, f"{line}\r\nHost: x\r\n{auth}Connection: close\r\n\r\n".encode())
            check(f"path {line.split()[1]!r} never 200/500", out[:12] != b"HTTP/1.1 200" and b" 500 " not in out[:20] and out.startswith(b"HTTP/"))
        # R6 error responses close the connection
        t0 = time.time()
        out = raw(port, b"GET /v1/state HTTP/1.1\r\nHost: x\r\n\r\n", timeout=5)
        check("401 closes connection", b" 401 " in out and time.time() - t0 < 2 and b"onnection: close" in out)
        # R7 log sanitising
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            raw(port, b"GET /v1/\x1b[31mevil HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            time.sleep(0.2)
        check("control chars escaped in logs", "\x1b" not in buf.getvalue() and "\\x1b" in buf.getvalue())
    finally:
        srv.shutdown()
        relay.KEEPALIVE_S = 15


def run_idle_timeout():
    import contextlib
    import io
    st = relay.State({"devices": {"a": {}}, "groups": {}})
    H = relay.make_handler(st, [TOKEN], False)
    H.timeout = 1
    srv = relay.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            sk = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=3)
            sk.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            sk.recv(500)
            time.sleep(1.6)
            sk.close()
    finally:
        srv.shutdown()
    check("idle keep-alive timeout not logged at info", "timed out" not in buf.getvalue() and '"GET /healthz' in buf.getvalue())
