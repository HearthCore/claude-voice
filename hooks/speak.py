#!/usr/bin/env python3
"""Claude Code hook -> claude-voice relay. Never blocks the session, never fails loudly.

Events: Stop (speaks last assistant message), Notification (speaks the notification).
Env: CLAUDE_VOICE_URL, CLAUDE_VOICE_TOKEN, CLAUDE_VOICE_TARGET (default: relay route),
     CLAUDE_VOICE_MAX_CHARS (default 500), CLAUDE_VOICE_TIMEOUT (default 2), CLAUDE_VOICE_RETRIES (default 1)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

STATE_DIR = os.path.expanduser(os.environ.get("CLAUDE_VOICE_STATE_DIR", "~/.claude-voice"))


def locally_muted(session_id: str) -> bool:
    return os.path.exists(os.path.join(STATE_DIR, "muted")) or (
        bool(session_id) and os.path.exists(os.path.join(STATE_DIR, f"muted.{session_id}")))


def last_assistant_text(path: str) -> str:
    last = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "assistant":
                    continue
                c = (o.get("message") or {}).get("content")
                if isinstance(c, str):
                    t = c
                else:
                    t = "\n".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                if t.strip():
                    last = t
    except OSError:
        return ""
    return last


def clean(text: str, max_chars: int) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)           # code blocks
    t = re.sub(r"`[^`]*`", " ", t)                              # inline code
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                 # images
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)              # links -> label
    t = re.sub(r"https?://\S+", " ", t)                         # bare urls
    t = "\n".join(l for l in t.splitlines() if not re.match(r"^\s*\|.*\|\s*$", l) and not re.match(r"^\s*[-:| ]{3,}$", l))
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)         # headings
    t = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", t, flags=re.M)      # list markers
    t = re.sub(r"[*_~>]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    m = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:m + 1] if m > max_chars // 3 else cut).strip()


def post(url: str, token: str, payload: dict, timeout: float, retries: int) -> bool:
    req = urllib.request.Request(url.rstrip("/") + "/v1/speak", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"} if token
                                 else {"Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            if e.code < 500:       # 4xx: retrying will not help
                return False
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4 * (attempt + 1))
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    url = os.environ.get("CLAUDE_VOICE_URL")
    if not url:
        return 0
    sid = str(data.get("session_id") or "")
    if locally_muted(sid):
        return 0
    event = data.get("hook_event_name", "")
    max_chars = int(os.environ.get("CLAUDE_VOICE_MAX_CHARS", "500"))
    if event == "Stop":
        if data.get("stop_hook_active"):
            return 0
        text = clean(last_assistant_text(data.get("transcript_path", "")), max_chars)
    elif event == "Notification":
        text = clean(str(data.get("message", "")), max_chars)
    else:
        return 0
    if not text:
        return 0
    payload = {"text": text, "session_id": sid, "source": "claude-code",
               "priority": "high" if event == "Notification" else "normal"}
    if os.environ.get("CLAUDE_VOICE_TARGET"):
        payload["target"] = os.environ["CLAUDE_VOICE_TARGET"]
    timeout = float(os.environ.get("CLAUDE_VOICE_TIMEOUT", "2"))
    retries = int(os.environ.get("CLAUDE_VOICE_RETRIES", "1"))
    token = os.environ.get("CLAUDE_VOICE_TOKEN", "")

    if hasattr(os, "fork") and not os.environ.get("CLAUDE_VOICE_SYNC"):
        if os.fork() != 0:       # parent returns immediately; session is never held up
            return 0
        os.setsid()
        sys.stdout = sys.stderr = open(os.devnull, "w")
    post(url, token, payload, timeout, retries)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)   # a voice hook must never break Claude Code
