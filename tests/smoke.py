#!/usr/bin/env python3
"""End-to-end smoke test (stdlib only): relay auth, group fan-out, mute, TTL, sidecar streaming, hook cleaning.
Run: python3 tests/smoke.py
"""
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from common import ROOT, TOKEN, PASSED, free_port, req, check, wait_up


def sse_collect(base, device, out, stop):
    r = urllib.request.Request(f"{base}/v1/stream?device={device}", headers={"Authorization": "Bearer " + TOKEN})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            etype = ""
            for raw in resp:
                line = raw.decode().rstrip("\n")
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:") and etype == "speak":
                    out.append(json.loads(line[5:]))
                if stop.is_set():
                    return
    except OSError:
        pass


def main():
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "relay.toml")
    open(cfg, "w").write('''
[defaults]
route = "active"
ttl_s = 45
[devices.a]
volume = 1.0
[devices.b]
volume = 0.5
latency_offset_ms = 800
[devices.c]
[devices.d]
[devices.e]
[groups]
home = ["a", "b"]
''')
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CLAUDE_VOICE_TOKEN": TOKEN}
    env.pop("LOCAL_UNSAFE_NO_AUTH", None)
    relay = subprocess.Popen([sys.executable, f"{ROOT}/relay/relay.py", "--config", cfg, "--port", str(port)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop = threading.Event()
    procs = [relay]
    try:
        wait_up(base)

        # --- auth
        check("healthz is open", urllib.request.urlopen(base + "/healthz").status == 200)
        check("no token -> 401", req(base, "GET", "/v1/state", token=None)[0] == 401)
        check("wrong token -> 401", req(base, "GET", "/v1/state", token="x" * 32)[0] == 401)
        check("right token -> 200", req(base, "GET", "/v1/state")[0] == 200)

        # --- startup safety
        r1 = subprocess.run([sys.executable, f"{ROOT}/relay/relay.py", "--port", "0"], env={k: v for k, v in env.items() if k != "CLAUDE_VOICE_TOKEN"},
                            capture_output=True, timeout=10)
        check("refuses to start without token", r1.returncode == 2)
        r2 = subprocess.run([sys.executable, f"{ROOT}/relay/relay.py", "--host", "0.0.0.0", "--port", "0"],
                            env={**env, "CLAUDE_VOICE_TOKEN": "", "LOCAL_UNSAFE_NO_AUTH": "true"}, capture_output=True, timeout=10)
        check("NO_AUTH refused on non-loopback", r2.returncode == 2)
        r3 = subprocess.run([sys.executable, f"{ROOT}/relay/relay.py", "--port", "0"], env={**env, "CLAUDE_VOICE_TOKEN": "short"},
                            capture_output=True, timeout=10)
        check("short token refused", r3.returncode == 2)

        # --- fan-out to group
        got = {"a": [], "b": [], "c": []}
        threads = []
        for d in got:
            t = threading.Thread(target=sse_collect, args=(base, d, got[d], stop), daemon=True)
            t.start()
            threads.append(t)
        time.sleep(0.6)
        st = req(base, "GET", "/v1/state")[1]
        check("all devices connected", all(st["devices"][d]["connected"] for d in "abc"))
        code, res = req(base, "POST", "/v1/speak", {"text": "Hallo Welt.", "target": "home", "session_id": "s1"})
        check("speak -> 202 with 2 events", code == 202 and len(res["events"]) == 2)
        time.sleep(0.5)
        check("group member a got event", len(got["a"]) == 1 and got["a"][0]["text"] == "Hallo Welt.")
        check("group member b got event with its own volume/offset", len(got["b"]) == 1 and got["b"][0]["volume"] == 0.5 and got["b"][0]["latency_offset_ms"] == 800)
        check("non-member c got nothing", len(got["c"]) == 0)
        check("ack works", req(base, "POST", "/v1/ack", {"id": got["a"][0]["id"], "device": "a", "status": "played"})[0] == 200)
        check("ack with wrong device rejected", req(base, "POST", "/v1/ack", {"id": got["a"][0]["id"], "device": "b"})[0] == 404)

        # --- mute scopes
        req(base, "POST", "/v1/mute", {"scope": "device", "id": "b", "muted": True})
        req(base, "POST", "/v1/speak", {"text": "Nur a.", "target": "home"})
        time.sleep(0.4)
        check("device mute: a hears, b muted", len(got["a"]) == 2 and len(got["b"]) == 1)
        req(base, "POST", "/v1/mute", {"scope": "device", "id": "b", "muted": False})
        req(base, "POST", "/v1/mute", {"scope": "session", "id": "s1", "muted": True})
        req(base, "POST", "/v1/speak", {"text": "Stumm.", "target": "home", "session_id": "s1"})
        time.sleep(0.4)
        check("session mute silences session", len(got["a"]) == 2)
        req(base, "POST", "/v1/mute", {"scope": "session", "id": "s1", "muted": False})
        req(base, "POST", "/v1/mute", {"scope": "group", "id": "home", "muted": True})
        req(base, "POST", "/v1/speak", {"text": "Gruppe stumm.", "target": "home"})
        time.sleep(0.4)
        check("group mute silences group", len(got["a"]) == 2)
        req(base, "POST", "/v1/mute", {"scope": "group", "id": "home", "muted": False})
        check("bad mute scope -> 400", req(base, "POST", "/v1/mute", {"scope": "nope", "id": "x"})[0] == 400)

        # --- routing
        code, res = req(base, "POST", "/v1/speak", {"text": "Aktiv."})
        check("default route 'active' picks exactly one device", code == 202 and len(res["events"]) == 1)
        check("unknown target -> 404", req(base, "POST", "/v1/speak", {"text": "x", "target": "nowhere"})[0] == 404)
        check("empty text -> 400", req(base, "POST", "/v1/speak", {"text": "  "})[0] == 400)

        # --- offline device + TTL
        stop.set()
        time.sleep(0.2)
        code, res = req(base, "POST", "/v1/speak", {"text": "Spaeter.", "target": "c", "ttl_s": 1})
        check("offline device: event queued", code == 202)
        time.sleep(1.3)
        late = []
        stop2 = threading.Event()
        t = threading.Thread(target=sse_collect, args=(base, "c", late, stop2), daemon=True)
        t.start()
        time.sleep(0.6)
        check("expired event is NOT delivered on reconnect", late == [])
        stop2.set()

        # --- sidecar with fake OpenAI-compatible TTS (streaming)
        seen = []

        class FakeTTS(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body, self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for _ in range(3):
                    chunk = b"\x00" * 100
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")

            def log_message(self, *a):
                pass

        tts_port = free_port()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", tts_port), FakeTTS)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sink = os.path.join(tmp, "player.out")
        senv = {**env, "CLAUDE_VOICE_URL": base, "CLAUDE_VOICE_DEVICE": "d", "TTS_URL": f"http://127.0.0.1:{tts_port}/v1",
                "TTS_KEY": "tts-secret", "TTS_VOICE": "testvoice", "CLAUDE_VOICE_PLAYER_CMD": f"tee {sink}"}
        sc = subprocess.Popen([sys.executable, f"{ROOT}/sidecar/sidecar.py"], env=senv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(sc)
        time.sleep(0.8)
        req(base, "POST", "/v1/speak", {"text": "Satz eins. Satz zwei. Satz drei.", "target": "d"})
        for _ in range(40):
            if len(seen) >= 3:
                break
            time.sleep(0.1)
        time.sleep(0.5)
        check("sidecar split into 3 sentences -> 3 TTS calls", len(seen) == 3)
        check("TTS call is OpenAI-compatible + streaming + auth", seen[0][0] == "/v1/audio/speech" and seen[0][1]["stream"] is True
              and seen[0][1]["voice"] == "testvoice" and seen[0][2] == "Bearer tts-secret")
        check("audio reached the player (3x300 bytes)", os.path.exists(sink) and os.path.getsize(sink) == 900)
        time.sleep(0.4)
        recent = req(base, "GET", "/v1/state")[1]["recent"]
        check("relay recorded sidecar ack 'played'", any(r["status"] == "played" for r in recent))
        srv.shutdown()

        # --- hook cleaning + sending
        sys.path.insert(0, f"{ROOT}/hooks")
        import importlib
        speak = importlib.import_module("speak")
        c = speak.clean("# Titel\n\nFertig, **Tests** gruen. Siehe [Doku](https://x.y/z) und `code`.\n\n```py\nprint(1)\n```\n| a | b |\n|---|---|\n- Punkt eins", 500)
        check("hook strips markdown/code/urls/tables", c == "Titel Fertig, Tests gruen. Siehe Doku und . Punkt eins" or ("```" not in c and "http" not in c and "|" not in c and "**" not in c))
        long = speak.clean("Satz. " * 200, 100)
        check("hook truncates at sentence boundary", len(long) <= 100 and long.endswith("."))

        tr = os.path.join(tmp, "t.jsonl")
        with open(tr, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Erste."}]}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "x"}, {"type": "text", "text": "Fertig, alles gruen."}]}}) + "\n")
        n_before = len(got["a"])
        stop3 = threading.Event()
        got_h = []
        t = threading.Thread(target=sse_collect, args=(base, "e", got_h, stop3), daemon=True)
        t.start()
        time.sleep(0.5)
        henv = {**env, "CLAUDE_VOICE_URL": base, "CLAUDE_VOICE_TARGET": "e", "CLAUDE_VOICE_STATE_DIR": os.path.join(tmp, "state")}
        hook = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=henv, timeout=10,
                              input=json.dumps({"hook_event_name": "Stop", "session_id": "cc1", "transcript_path": tr}).encode())
        check("hook exits 0 immediately", hook.returncode == 0)
        time.sleep(1.0)
        check("hook delivered last assistant text (not an older one)", len(got_h) == 1 and got_h[0]["text"] == "Fertig, alles gruen.")
        subprocess.run([sys.executable, f"{ROOT}/tools/voicectl.py", "local", "mute"], env=henv, check=True, capture_output=True)
        subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=henv, timeout=10,
                       input=json.dumps({"hook_event_name": "Stop", "session_id": "cc1", "transcript_path": tr}).encode())
        time.sleep(0.8)
        check("local mute flag silences hook", len(got_h) == 1)
        dead = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env={**henv, "CLAUDE_VOICE_URL": "http://127.0.0.1:1", "CLAUDE_VOICE_STATE_DIR": os.path.join(tmp, "s2"), "CLAUDE_VOICE_SYNC": "1"},
                              timeout=15, input=json.dumps({"hook_event_name": "Stop", "session_id": "z", "transcript_path": tr}).encode())
        check("hook never fails when relay is down", dead.returncode == 0)
        stop3.set()
    finally:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
    import test_relay
    test_relay.run()
    print(f"\n{len(PASSED)} checks passed")


if __name__ == "__main__":
    main()
