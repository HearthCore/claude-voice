"""Log levels: parsing, relay warn filtering, content only at debug, secrets never."""
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

from common import ROOT, check, free_port, req, wait_up
from test_relay import wait_for

sys.path.insert(0, os.path.join(ROOT, "relay"))
sys.path.insert(0, os.path.join(ROOT, "sidecar"))
import relay  # noqa: E402
import sidecar  # noqa: E402

RTOKEN = "Rk9sT3ken-secret-0123456789abcdef"
TTSKEY = "ttskey-SECRET-9876"
HDRVAL = "hdrvalue-SECRET-5555"
TEXT = "Geheimtext Zebra 4711."


def stop(p):
    p.send_signal(signal.SIGTERM)
    try:
        return p.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return p.communicate()[0]


def scenario(level):
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "relay.toml")
    open(cfg, "w").write('[defaults]\nroute = "d"\n[devices.d]\n')
    seen = []

    class TTS(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "audio/pcm")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"\0" * 100)

        def log_message(self, *a):
            pass

    tts = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), TTS)
    threading.Thread(target=tts.serve_forever, daemon=True).start()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CLAUDE_VOICE_TOKEN": RTOKEN, "CLAUDE_VOICE_LOG_LEVEL": level}
    env.pop("LOCAL_UNSAFE_NO_AUTH", None)
    rp = subprocess.Popen([sys.executable, f"{ROOT}/relay/relay.py", "--config", cfg, "--port", str(port)], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_up(base)
    sp = subprocess.Popen([sys.executable, f"{ROOT}/sidecar/sidecar.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**env, "CLAUDE_VOICE_URL": base, "CLAUDE_VOICE_DEVICE": "d", "TTS_KEY": TTSKEY,
                               "TTS_URL": f"http://127.0.0.1:{tts.server_address[1]}/v1", "CLAUDE_VOICE_PLAYER_CMD": "tee /dev/null",
                               "TTS_EXTRA_HEADERS": f"X-Secret: {HDRVAL}"})
    hdir = os.path.join(tmp, "hook")
    try:
        wait_for(lambda: req(base, "GET", "/v1/state", token=RTOKEN)[1]["devices"]["d"]["connected"], 6)
        subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], timeout=10, capture_output=True,
                       env={**env, "CLAUDE_VOICE_URL": base, "CLAUDE_VOICE_SYNC": "1", "CLAUDE_VOICE_STATE_DIR": hdir},
                       input=json.dumps({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": TEXT}).encode())
        wait_for(lambda: seen, 6)
        time.sleep(0.8)
    finally:
        sout, rout = stop(sp), stop(rp)
        tts.shutdown()
    hp = os.path.join(hdir, "hook.log")
    return rout, sout, open(hp, encoding="utf-8").read() if os.path.exists(hp) else ""


def run():
    check("relay/sidecar level parsing: invalid -> default", relay.parse_level("bogus", "info") == (3, False)
          and relay.parse_level("DEBUG", "info") == (4, True) and sidecar.parse_level("", "info") == (3, True)
          and sidecar.parse_level("nope")[1] is False)
    for lvl in ("debug", "info"):
        rout, sout, hlog = scenario(lvl)
        blob = rout + sout + hlog
        check(f"{lvl}: no secrets in relay/sidecar/hook logs", not any(x in blob for x in (RTOKEN, TTSKEY, HDRVAL)) and "Bearer" not in blob)
        if lvl == "debug":
            check("debug: text in relay, sidecar and hook logs", TEXT in rout and TEXT in sout and TEXT in hlog)
            check("debug: debug warning at startup", "message text is written to the log" in rout and "message text is written to the log" in sout)
            check("debug: sidecar shows ttfb, content-type, header names", "ttfb=" in sout and "content-type=audio/pcm" in sout
                  and "X-Secret" in sout and "player argv=" in sout)
        else:
            check("info: message text in no log", "Zebra" not in blob and "4711" not in blob)
            check("info: operational lines present", "speak target=d chars=" in rout and "-> played" in sout)
    # warn level hides info request lines but keeps 4xx
    port = free_port()
    env = {**os.environ, "CLAUDE_VOICE_TOKEN": RTOKEN, "CLAUDE_VOICE_LOG_LEVEL": "warn"}
    env.pop("LOCAL_UNSAFE_NO_AUTH", None)
    rp = subprocess.Popen([sys.executable, f"{ROOT}/relay/relay.py", "--port", str(port)], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_up(f"http://127.0.0.1:{port}")
    req(f"http://127.0.0.1:{port}", "GET", "/v1/state", token=RTOKEN)
    req(f"http://127.0.0.1:{port}", "GET", "/v1/state", token=None)
    out = stop(rp)
    check("relay at warn: no info lines, 401 still logged", '" 200 ' not in out and "401" in out and "claude-voice relay" not in out)
    rp = subprocess.Popen([sys.executable, f"{ROOT}/relay/relay.py", "--port", "0"], env={**env, "CLAUDE_VOICE_LOG_LEVEL": "off", "CLAUDE_VOICE_TOKEN": "short"},
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = rp.communicate(timeout=10)[0]
    check("level off still prints FATAL", "FATAL" in out)
