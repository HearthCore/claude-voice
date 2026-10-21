#!/usr/bin/env python3
"""voicectl: control claude-voice. Env: CLAUDE_VOICE_URL, CLAUDE_VOICE_TOKEN.

  voicectl status
  voicectl speak "Text" [--target home]
  voicectl mute|unmute [--scope all|session|device|group] [--id NAME]     (relay-side)
  voicectl local mute|unmute [--session SID]                              (client-side flag, instant, offline-safe)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

STATE_DIR = os.path.expanduser(os.environ.get("CLAUDE_VOICE_STATE_DIR", "~/.claude-voice"))


def call(method: str, path: str, body: dict | None = None) -> dict:
    url = os.environ.get("CLAUDE_VOICE_URL")
    if not url:
        sys.exit("CLAUDE_VOICE_URL not set")
    h = {"Content-Type": "application/json"}
    if os.environ.get("CLAUDE_VOICE_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["CLAUDE_VOICE_TOKEN"]
    req = urllib.request.Request(url.rstrip("/") + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except OSError as e:
        sys.exit(f"relay unreachable: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("speak"); s.add_argument("text"); s.add_argument("--target")
    for n in ("mute", "unmute"):
        m = sub.add_parser(n); m.add_argument("--scope", default="all"); m.add_argument("--id", default="*")
    lo = sub.add_parser("local"); lo.add_argument("action", choices=["mute", "unmute"]); lo.add_argument("--session")
    a = ap.parse_args()

    if a.cmd == "status":
        print(json.dumps(call("GET", "/v1/state"), indent=2))
    elif a.cmd == "speak":
        b = {"text": a.text, "source": "voicectl"}
        if a.target:
            b["target"] = a.target
        print(json.dumps(call("POST", "/v1/speak", b)))
    elif a.cmd in ("mute", "unmute"):
        print(json.dumps(call("POST", "/v1/mute", {"scope": a.scope, "id": a.id, "muted": a.cmd == "mute"})))
    elif a.cmd == "local":
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, f"muted.{a.session}" if a.session else "muted")
        if a.action == "mute":
            open(p, "w").close()
        elif os.path.exists(p):
            os.remove(p)
        print(f"local {a.action}d" + (f" (session {a.session})" if a.session else ""))


if __name__ == "__main__":
    main()
