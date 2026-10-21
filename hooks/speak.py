#!/usr/bin/env python3
"""Claude Code hook -> claude-voice relay. Never blocks the session, never fails loudly.

Events: Stop (speaks `last_assistant_message`, falls back to parsing the transcript), Notification (speaks `message`).
Env: CLAUDE_VOICE_URL, CLAUDE_VOICE_TOKEN, CLAUDE_VOICE_TARGET (default: relay route),
     CLAUDE_VOICE_MAX_CHARS (default 500), CLAUDE_VOICE_TIMEOUT (default 2), CLAUDE_VOICE_RETRIES (default 1)
     CLAUDE_VOICE_LOG_LEVEL=off|error|warn|info|debug (default off): JSON lines appended to
     $CLAUDE_VOICE_STATE_DIR/hook.log (0600, rotated at 256 KiB to hook.log.1). info = decision + metadata + char
     count; debug adds raw/cleaned text and the posted payload. Local file only; token is never logged.
"""
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

STATE_DIR = os.path.expanduser(os.environ.get("CLAUDE_VOICE_STATE_DIR", "~/.claude-voice"))
META_KEYS = ("hook_event_name", "session_id", "prompt_id", "stop_reason", "stop_hook_active",
             "agent_id", "agent_type", "notification_type", "permission_mode")
LEVELS = {"off": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}
LOG_MAX_BYTES = 256 * 1024
_warned = False


def log(level: str, event: str, **fields) -> None:
    global _warned
    try:
        v = os.environ.get("CLAUDE_VOICE_LOG_LEVEL", "").strip().lower()
        bad = bool(v) and v not in LEVELS
        if bad and not _warned:
            _warned = True
            _write("warn", "log", msg="invalid CLAUDE_VOICE_LOG_LEVEL, using off")
        if LEVELS[level] <= LEVELS.get(v, 0):
            _write(level, event, **fields)
    except Exception:
        pass


def _write(level: str, event: str, **fields) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    p = os.path.join(STATE_DIR, "hook.log")
    if os.path.exists(p) and os.path.getsize(p) > LOG_MAX_BYTES:
        os.replace(p, p + ".1")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level, "event": event, **fields}
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def decide(data: dict, decision: str, source: str = "", raw: str = "", text: str = "", payload: dict | None = None) -> None:
    log("info", "decision", decision=decision, chars=len(text), source=source, keys=sorted(data),
        input={k: data.get(k) for k in META_KEYS if k in data})
    log("debug", "content", decision=decision, source=source, raw=raw[:2000], text=text, payload=payload)


def creds_ok(url: str) -> bool:
    """Tokens only go over https, or to a loopback host."""
    try:
        u = urlsplit(url)
        host = u.hostname or ""
    except ValueError:
        return False
    if u.scheme == "https":
        return True
    try:
        return u.scheme == "http" and (host == "localhost" or ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):   # a redirect must never carry the token along
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def env_num(name: str, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


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


RESULT = ["no attempt"]


def post(url: str, token: str, payload: dict, timeout: float, retries: int) -> bool:
    req = urllib.request.Request(url.rstrip("/") + "/v1/speak", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"} if token
                                 else {"Content-Type": "application/json"})
    for attempt in range(retries + 1):
        try:
            with OPENER.open(req, timeout=timeout) as r:
                RESULT[0] = f"HTTP {r.status}"
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            RESULT[0] = f"HTTP {e.code}"
            if e.code < 500:       # 4xx: retrying will not help
                return False
        except (urllib.error.URLError, OSError) as e:
            RESULT[0] = f"error: {type(e).__name__}"
        time.sleep(0.4 * (attempt + 1))
    return False


def deliver(url: str, token: str, payload: dict, timeout: float, retries: int) -> None:
    ok = post(url, token, payload, timeout, retries)
    log("info" if ok else "warn", "post", result=RESULT[0], ok=ok)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    url = os.environ.get("CLAUDE_VOICE_URL")
    if not url:
        return 0
    if not isinstance(data, dict):
        return 0
    sid = str(data.get("session_id") or "")
    if locally_muted(sid):
        decide(data, "skip:local_mute")
        return 0
    event = data.get("hook_event_name", "")
    max_chars = env_num("CLAUDE_VOICE_MAX_CHARS", "500", int)
    if event == "Stop":
        if data.get("stop_hook_active"):
            decide(data, "skip:stop_hook_active")
            return 0
        raw, source = data.get("last_assistant_message"), "last_assistant_message"
        if not isinstance(raw, str) or not raw.strip():
            raw, source = last_assistant_text(str(data.get("transcript_path") or "")), "transcript"
        text = clean(raw, max_chars)
    elif event == "Notification":
        raw, source = str(data.get("message", "")), "message"
        text = clean(raw, max_chars)
    else:
        decide(data, "skip:event")
        return 0
    if not text:
        decide(data, "skip:empty", source, raw)
        return 0
    payload = {"text": text, "session_id": sid, "source": "claude-code",
               "priority": "high" if event == "Notification" else "normal"}
    if os.environ.get("CLAUDE_VOICE_TARGET"):
        payload["target"] = os.environ["CLAUDE_VOICE_TARGET"]
    timeout = env_num("CLAUDE_VOICE_TIMEOUT", "2", float)
    retries = max(0, env_num("CLAUDE_VOICE_RETRIES", "1", int))
    token = os.environ.get("CLAUDE_VOICE_TOKEN", "")

    if token and not creds_ok(url):
        decide(data, "skip:insecure_url", source, raw, text)
        return 0
    decide(data, "send", source, raw, text, payload)
    if hasattr(os, "fork") and not os.environ.get("CLAUDE_VOICE_SYNC"):
        if os.fork() != 0:       # parent returns immediately; session is never held up
            return 0
        try:
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):     # drop the pipes to Claude Code, or it waits for them to close
                os.dup2(null, fd)
            os.setsid()
            deliver(url, token, payload, timeout, retries)
        finally:
            os._exit(0)
    deliver(url, token, payload, timeout, retries)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("error", "crash", error=type(e).__name__)
        sys.exit(0)   # a voice hook must never break Claude Code
