"""Hook tests: fd hygiene, last_assistant_message, edge cases."""
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

from common import ROOT, check, free_port
from test_relay import wait_for

sys.path.insert(0, os.path.join(ROOT, "hooks"))
import speak  # noqa: E402


def run_hook(payload, env, timeout=10):
    t = time.time()
    p = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=env, capture_output=True, timeout=timeout,
                       input=json.dumps(payload).encode())
    return p, time.time() - t


def run():
    posts = []

    class Cap(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    cap = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Cap)
    threading.Thread(target=cap.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{cap.server_address[1]}", "CLAUDE_VOICE_STATE_DIR": tmp}
    env.pop("CLAUDE_VOICE_SYNC", None)
    for k in ("CLAUDE_VOICE_MAX_CHARS", "CLAUDE_VOICE_TARGET"):
        env.pop(k, None)

    # black hole: accepts, never answers. Hook must release Claude Code's pipes at once.
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(16)
    held = []
    threading.Thread(target=lambda: [held.append(hole.accept()) for _ in iter(int, 1)], daemon=True).start()
    henv = {**env, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{hole.getsockname()[1]}", "CLAUDE_VOICE_TIMEOUT": "2", "CLAUDE_VOICE_RETRIES": "1"}
    p, dt = run_hook({"hook_event_name": "Notification", "message": "Hallo."}, henv)
    print(f"hook with captured pipes vs black-hole server: {dt:.2f}s")
    check("hook returns < 1s with captured pipes (fds released)", p.returncode == 0 and dt < 1.0)

    def sent(n, secs=3):
        return wait_for(lambda: len(posts) >= n, secs)

    p, _ = run_hook({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "Neu und fertig.",
                     "transcript_path": "/nonexistent"}, env)
    check("Stop uses last_assistant_message", sent(1) and posts[-1]["text"] == "Neu und fertig.")

    tr = os.path.join(tmp, "t.jsonl")
    open(tr, "w").write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Aus dem Transkript."}]}}) + "\n")
    run_hook({"hook_event_name": "Stop", "session_id": "s", "transcript_path": tr}, env)
    check("Stop falls back to transcript when field missing", sent(2) and posts[-1]["text"] == "Aus dem Transkript.")

    n = len(posts)
    run_hook({"hook_event_name": "Stop", "session_id": "s",
              "last_assistant_message": "```py\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"}, env)
    time.sleep(0.8)
    check("code block + table only -> nothing sent", len(posts) == n)

    long = "Das ist ein ganzer Satz. " * 100
    run_hook({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": long}, {**env, "CLAUDE_VOICE_MAX_CHARS": "120"})
    check("long answer cut at sentence boundary", sent(n + 1) and len(posts[-1]["text"]) <= 120 and posts[-1]["text"].endswith("Satz."))

    uni = "Grüße, Käse, Ünïcødé – schön."
    check("umlauts survive clean()", speak.clean("**" + uni + "**", 500) == uni)
    run_hook({"hook_event_name": "Notification", "message": uni}, env)
    check("umlauts arrive at relay", sent(n + 2) and posts[-1]["text"] == uni and posts[-1]["priority"] == "high")

    for bad in ("abc", "-5", ""):
        p, _ = run_hook({"hook_event_name": "Stop", "last_assistant_message": "Ok."}, {**env, "CLAUDE_VOICE_MAX_CHARS": bad})
        if bad == "abc":
            check("invalid CLAUDE_VOICE_MAX_CHARS -> exit 0", p.returncode == 0)
    p, _ = run_hook({"hook_event_name": "Stop", "last_assistant_message": "Ok."},
                    {**env, "CLAUDE_VOICE_TIMEOUT": "x", "CLAUDE_VOICE_RETRIES": "y"})
    check("invalid TIMEOUT/RETRIES -> exit 0", p.returncode == 0)
    p = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=env, input=b"\xff not json", capture_output=True, timeout=10)
    check("garbage stdin -> exit 0", p.returncode == 0)
    check("hooks.json entries are async", all(h["async"] is True and h["timeout"] == 10
          for ev in json.load(open(f"{ROOT}/hooks/hooks.json"))["hooks"].values() for g in ev for h in g["hooks"]))
    cap.shutdown()
