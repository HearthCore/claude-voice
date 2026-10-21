#!/usr/bin/env python3
"""claude-voice sidecar: subscribes to the relay (outbound SSE), synthesizes via an
OpenAI-compatible /v1/audio/speech endpoint with streaming, plays audio locally.

Env:
  CLAUDE_VOICE_URL, CLAUDE_VOICE_TOKEN   relay (token optional only with LOCAL_UNSAFE_NO_AUTH relay)
  CLAUDE_VOICE_DEVICE                    device name as in relay config (required)
  TTS_URL   (default http://127.0.0.1:8880/v1)   TTS_KEY   TTS_MODEL (kokoro)   TTS_VOICE (af_heart)
  TTS_FORMAT (mp3|wav|pcm|opus, default mp3)
  CLAUDE_VOICE_PLAYER_CMD   override player, e.g. "mpv --no-terminal --really-quiet -"
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import queue
import urllib.error
import urllib.request
from datetime import datetime, timezone

RELAY = os.environ.get("CLAUDE_VOICE_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("CLAUDE_VOICE_TOKEN", "")
DEVICE = os.environ.get("CLAUDE_VOICE_DEVICE", "")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8880/v1").rstrip("/")
TTS_KEY = os.environ.get("TTS_KEY", "")
TTS_MODEL = os.environ.get("TTS_MODEL", "kokoro")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
TTS_FORMAT = os.environ.get("TTS_FORMAT", "mp3")


def log(m: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {m}", flush=True)


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def split_sentences(text: str, max_len: int = 240) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: list[str] = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(", ", 0, max_len)
            cut = cut if cut > 40 else max_len
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:]
        if p.strip():
            out.append(p.strip())
    return out


def player_cmd() -> list[str]:
    if os.environ.get("CLAUDE_VOICE_PLAYER_CMD"):
        return shlex.split(os.environ["CLAUDE_VOICE_PLAYER_CMD"])
    if shutil.which("mpv"):
        return ["mpv", "--no-terminal", "--really-quiet", "--cache=no", "-"]
    if shutil.which("ffplay"):
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-fflags", "nobuffer", "-i", "pipe:0"]
    raise RuntimeError("no player found (install mpv or ffmpeg, or set CLAUDE_VOICE_PLAYER_CMD)")


def tts_stream(sentence: str, volume: float):
    body = {"model": TTS_MODEL, "voice": TTS_VOICE, "input": sentence,
            "response_format": TTS_FORMAT, "stream": True}
    h = {"Content-Type": "application/json"}
    if TTS_KEY:
        h["Authorization"] = f"Bearer {TTS_KEY}"
    req = urllib.request.Request(TTS_URL + "/audio/speech", data=json.dumps(body).encode(), headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        while True:
            chunk = r.read(4096)
            if not chunk:
                return
            yield chunk


def speak(ev: dict) -> str:
    sentences = split_sentences(ev["text"])
    play_at = ev.get("play_at")
    if play_at:
        try:
            t = datetime.fromisoformat(play_at).timestamp() - ev.get("latency_offset_ms", 0) / 1000
            time.sleep(max(0.0, min(t - time.time(), 5.0)))
        except ValueError:
            pass
    proc = subprocess.Popen(player_cmd(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    q: queue.Queue = queue.Queue(maxsize=64)
    err: list[str] = []

    def fetch():
        try:
            for s in sentences:
                for chunk in tts_stream(s, ev.get("volume", 1.0)):
                    q.put(chunk)
        except (urllib.error.URLError, OSError) as e:
            err.append(str(e))
        finally:
            q.put(None)

    threading.Thread(target=fetch, daemon=True).start()
    try:
        while (chunk := q.get()) is not None:
            proc.stdin.write(chunk)
        proc.stdin.close()
        proc.wait(timeout=120)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        proc.kill()
    return "tts_error" if err else "played"


def ack(evid: str, status: str) -> None:
    req = urllib.request.Request(RELAY + "/v1/ack", method="POST",
                                 data=json.dumps({"id": evid, "device": DEVICE, "status": status}).encode(),
                                 headers={"Content-Type": "application/json", **auth_headers()})
    try:
        urllib.request.urlopen(req, timeout=3).close()
    except (urllib.error.URLError, OSError):
        pass


def handle(ev: dict) -> None:
    log(f"speak {ev['id']} ({len(ev['text'])} chars)")
    status = speak(ev)
    ack(ev["id"], status)
    log(f"{ev['id']} -> {status}")


def run_once() -> None:
    req = urllib.request.Request(f"{RELAY}/v1/stream?device={DEVICE}", headers={"Accept": "text/event-stream", **auth_headers()})
    with urllib.request.urlopen(req, timeout=60) as r:   # keepalive every 15s keeps this alive
        log(f"connected to {RELAY} as {DEVICE}")
        etype, data = "", []
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith(":"):
                continue
            if line == "":
                if etype == "speak" and data:
                    try:
                        handle(json.loads("\n".join(data)))
                    except Exception as e:   # one bad event must not kill the loop
                        log(f"event error: {e}")
                etype, data = "", []
            elif line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())


def main() -> int:
    if not DEVICE:
        log("FATAL: CLAUDE_VOICE_DEVICE not set")
        return 2
    backoff = 1.0
    while True:
        try:
            run_once()
            backoff = 1.0
        except urllib.error.HTTPError as e:
            log(f"relay HTTP {e.code} (check token / device name)")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"relay unreachable: {e}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
