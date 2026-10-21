"""Shared helpers for tests (stdlib only)."""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "t" * 32
PASSED = []
# test processes may run under a real claude; hooks must still speak and notifications must pass
os.environ.setdefault("CLAUDE_VOICE_NESTED", "speak")
os.environ.setdefault("CLAUDE_VOICE_NOTIFY_TYPES", "all")
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base, method, path, body=None, token=TOKEN):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(base + path, method=method, headers=h, data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, None


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)
    PASSED.append(name)


def wait_up(base):
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    sys.exit("relay did not start")
