"""claude-voice sidecar: subscribes to the relay (outbound SSE), synthesizes via an
OpenAI-compatible /v1/audio/speech endpoint with streaming, plays audio locally.

Env:
  CLAUDE_VOICE_URL, CLAUDE_VOICE_TOKEN   relay (token optional only with LOCAL_UNSAFE_NO_AUTH relay)
  CLAUDE_VOICE_DEVICE                    device name as in relay config (required)
  TTS_URL   (default http://127.0.0.1:8880/v1)   TTS_KEY   TTS_MODEL (kokoro)   TTS_VOICE (af_heart)
  TTS_FORMAT (pcm|mp3|wav|opus, default pcm)
  TTS_PCM_RATE (default 24000)  TTS_PCM_CHANNELS (default 1)   raw PCM is s16le; used when the TTS answers
                                audio/pcm or audio/l16 (or no/non-audio content type and TTS_FORMAT=pcm)
  TTS_RETRIES (default 2)    retries per sentence on HTTP 5xx / connection error before the first byte
  TTS_EXTRA_HEADERS          extra TTS request headers, "Name: value; Name2: value2" (e.g. Pangolin
                             P-Access-Token-Id / P-Access-Token). Invalid names or CR/LF values are
                             ignored (logged once, values never logged).
  CLAUDE_VOICE_PLAYER_CMD    override player (bypasses selection), e.g. "mpv --no-terminal --really-quiet -"

The player is started lazily after the first successful TTS response and chosen by its Content-Type.
Events past their TTL are not spoken (ack "expired"); credentials are only sent over https or to loopback,
and never follow redirects. CLAUDE_VOICE_PLAYER_DEADLINE_S (default 180) kills a stalled player.
"""
from __future__ import annotations

import functools
import ipaddress
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
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def log(m: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {m}", flush=True)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        log(f"{name}: invalid value, using {default}")
        return default


RELAY = os.environ.get("CLAUDE_VOICE_URL", "http://127.0.0.1:8787").rstrip("/")
TOKEN = os.environ.get("CLAUDE_VOICE_TOKEN", "")
DEVICE = os.environ.get("CLAUDE_VOICE_DEVICE", "")
TTS_URL = os.environ.get("TTS_URL", "http://127.0.0.1:8880/v1").rstrip("/")
TTS_KEY = os.environ.get("TTS_KEY", "")
TTS_MODEL = os.environ.get("TTS_MODEL", "kokoro")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
TTS_FORMAT = os.environ.get("TTS_FORMAT", "pcm").lower()
TTS_PCM_RATE = max(1, env_int("TTS_PCM_RATE", 24000))
TTS_PCM_CHANNELS = max(1, env_int("TTS_PCM_CHANNELS", 1))
TTS_RETRIES = max(0, env_int("TTS_RETRIES", 2))
TTS_SENTENCE_DEADLINE_S = 60   # total time budget per sentence incl. retries
PLAYER_DEADLINE_S = max(1, env_int("CLAUDE_VOICE_PLAYER_DEADLINE_S", 180))   # total per event; then the player is killed
MAX_PLAY_AT_WAIT_S = 5   # events are handled sequentially, so this is only a nudge; real sync is roadmap v0.3
RAW_PCM_TYPES = ("audio/pcm", "audio/l16")


def creds_ok(url: str) -> bool:
    """Credentials only go over https, or to a loopback host."""
    try:
        u = urllib.parse.urlsplit(url)
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
    def redirect_request(self, *a, **kw):   # a redirect must never carry credentials along
        return None


OPENER = urllib.request.build_opener(NoRedirect)
HEADER_DENYLIST = {"authorization", "host", "content-length", "transfer-encoding", "content-type", "connection"}


def parse_extra_headers(spec: str) -> dict:
    out: dict = {}
    for i, part in enumerate(p for p in spec.split(";") if p.strip()):
        name, sep, value = part.partition(":")
        name, value = name.strip(), value.strip()
        if (not sep or not re.fullmatch(r"[A-Za-z0-9-]+", name) or name.lower() in HEADER_DENYLIST
                or not re.fullmatch(r"[\x20-\x7e]*", value)):
            log(f"TTS_EXTRA_HEADERS: ignoring invalid entry #{i + 1}")
            continue
        out[name] = value
    return out


EXTRA_HEADERS = parse_extra_headers(os.environ.get("TTS_EXTRA_HEADERS", ""))


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


def base_type(ct: str | None) -> str:
    return (ct or "").split(";")[0].strip().lower()


def is_raw_pcm(ct: str | None, fmt: str | None = None) -> bool:
    t = base_type(ct)
    if t in RAW_PCM_TYPES:
        return True
    return (fmt or TTS_FORMAT) == "pcm" and not t.startswith("audio/")


@functools.lru_cache(maxsize=1)
def ffplay_has_ch_layout() -> bool:
    try:
        return b"-ch_layout" in subprocess.run(["ffplay", "-hide_banner", "-h"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def player_cmd(ct: str | None = None, fmt: str | None = None, rate: int | None = None, ch: int | None = None) -> list[str]:
    if os.environ.get("CLAUDE_VOICE_PLAYER_CMD"):
        return shlex.split(os.environ["CLAUDE_VOICE_PLAYER_CMD"])
    raw = is_raw_pcm(ct, fmt)
    rate, ch = rate or TTS_PCM_RATE, ch or TTS_PCM_CHANNELS
    if shutil.which("mpv"):
        cmd = ["mpv", "--no-terminal", "--really-quiet", "--cache=no"]
        if raw:
            cmd += ["--demuxer=rawaudio", "--demuxer-rawaudio-format=s16le", f"--demuxer-rawaudio-rate={rate}",
                    f"--demuxer-rawaudio-channels={ch}"]
        return cmd + ["-"]
    if shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-fflags", "nobuffer"]
        if raw:
            cmd += ["-f", "s16le", "-ar", str(rate)]
            cmd += ["-ch_layout", "mono" if ch == 1 else "stereo"] if ch in (1, 2) and ffplay_has_ch_layout() else ["-ac", str(ch)]
        return cmd + ["-i", "pipe:0"]
    raise RuntimeError("no player found (install mpv or ffmpeg, or set CLAUDE_VOICE_PLAYER_CMD)")


class TTSError(Exception):
    pass


def tts_open(sentence: str, deadline: float | None = None):
    """POST one sentence; retry 5xx/connection errors before the first byte. Returns (response, content_type, first_chunk)."""
    body = {"model": TTS_MODEL, "voice": TTS_VOICE, "input": sentence, "response_format": TTS_FORMAT, "stream": True}
    h = {"Content-Type": "application/json", **EXTRA_HEADERS}
    if TTS_KEY:
        h["Authorization"] = f"Bearer {TTS_KEY}"
    if (TTS_KEY or EXTRA_HEADERS) and not creds_ok(TTS_URL):
        raise TTSError("refusing to send TTS credentials over plain http to a remote host")
    deadline = deadline or time.monotonic() + TTS_SENTENCE_DEADLINE_S
    last = "unknown"
    for attempt in range(TTS_RETRIES + 1):
        if time.monotonic() > deadline:
            raise TTSError("deadline exceeded")
        if attempt:
            time.sleep(0.3 * attempt)
        req = urllib.request.Request(TTS_URL + "/audio/speech", data=json.dumps(body).encode(), headers=h, method="POST")
        r = None
        try:
            r = OPENER.open(req, timeout=30)
            first = r.read(4096)
            if first:
                return r, r.headers.get("Content-Type"), first
            last = "empty response"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code < 500:
                raise TTSError(last) from None
        except (urllib.error.URLError, OSError) as e:
            last = type(e).__name__
        if r is not None:
            r.close()
    raise TTSError(f"{last} after {TTS_RETRIES + 1} attempts")


def parse_ts(v) -> datetime | None:
    try:
        t = datetime.fromisoformat(v)
    except (ValueError, TypeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def is_stale(ev: dict) -> bool:
    if ev.get("expires_in_s") is not None:   # relative TTL + local monotonic receive time: no wall-clock dependence
        try:
            return time.monotonic() - ev.get("_recv", time.monotonic()) > float(ev["expires_in_s"])
        except (TypeError, ValueError):
            return False
    created = parse_ts(ev.get("created"))
    try:
        ttl = float(ev.get("ttl_s"))
    except (TypeError, ValueError):
        return False
    return created is not None and time.time() > created.timestamp() + ttl


def speak(ev: dict) -> str:
    sentences = split_sentences(ev["text"])
    stop = threading.Event()
    q: queue.Queue = queue.Queue(maxsize=64)
    err: list[str] = []

    def put(item) -> bool:
        while not stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except queue.Full:
                pass
        return False

    def fetch():
        first_ct = None
        try:
            for s in sentences:
                if stop.is_set():
                    return
                dl = time.monotonic() + TTS_SENTENCE_DEADLINE_S
                r, ct, chunk = tts_open(s, dl)
                with r:
                    if first_ct is None:
                        first_ct = base_type(ct)
                        if not put(("start", ct)):
                            return
                    elif base_type(ct) != first_ct:
                        log(f"content-type changed ({base_type(ct)!r} vs {first_ct!r}), skipping sentence")
                        continue
                    got = 0
                    while chunk:
                        got += len(chunk)
                        if not put(("data", chunk)):
                            return
                        if time.monotonic() > dl:
                            raise TTSError("deadline exceeded")
                        chunk = r.read(4096)
                    want = r.headers.get("Content-Length")
                    if want and want.isdigit() and got != int(want):   # http.client does not flag short bodies on read(n)
                        raise TTSError("truncated response")
        except TTSError as e:
            err.append(str(e))
        except Exception as e:   # incl. HTTPException / IncompleteRead: truncated audio is never "played"
            err.append(type(e).__name__)
        finally:
            put(("end", None))

    threading.Thread(target=fetch, daemon=True).start()
    play_at = parse_ts(ev.get("play_at"))
    if play_at:
        t = play_at.timestamp() - (ev.get("latency_offset_ms") or 0) / 1000
        time.sleep(max(0.0, min(t - time.time(), MAX_PLAY_AT_WAIT_S)))
    proc = None
    timer = None
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        if proc and proc.poll() is None:
            proc.kill()

    try:
        while True:
            kind, val = q.get()
            if kind == "end":
                break
            if kind == "start":
                try:
                    proc = subprocess.Popen(player_cmd(val), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, RuntimeError) as e:
                    log(f"player start failed: {e}")
                    return "player_error"
                timer = threading.Timer(PLAYER_DEADLINE_S, kill)
                timer.daemon = True
                timer.start()
            elif proc:
                proc.stdin.write(val)
        if proc:
            proc.stdin.close()
            proc.wait(timeout=PLAYER_DEADLINE_S)
        if timed_out.is_set():
            log("player deadline exceeded")
            return "player_error"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        log("player died or hung")
        return "player_error"
    finally:
        stop.set()
        if timer:
            timer.cancel()
        if proc:
            try:
                proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    if err:
        log(f"tts error: {err[0]}")
    return "tts_error" if err else "played"


def ack(evid: str, status: str) -> None:
    req = urllib.request.Request(RELAY + "/v1/ack", method="POST",
                                 data=json.dumps({"id": evid, "device": DEVICE, "status": status}).encode(),
                                 headers={"Content-Type": "application/json", **auth_headers()})
    try:
        OPENER.open(req, timeout=3).close()
    except Exception:
        pass


def handle(ev: dict) -> None:
    log(f"speak {ev['id']} ({len(ev['text'])} chars)")
    status = "expired" if is_stale(ev) else speak(ev)
    ack(ev["id"], status)
    log(f"{ev['id']} -> {status}")


def run_once() -> None:
    url = f"{RELAY}/v1/stream?device={urllib.parse.quote(DEVICE, safe='')}"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream", **auth_headers()})
    with OPENER.open(req, timeout=60) as r:   # keepalive every 15s keeps this alive
        log(f"connected to {RELAY} as {DEVICE}")
        etype, data = "", []
        for raw in r:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith(":"):
                continue
            if line == "":
                if etype == "speak" and data:
                    try:
                        ev = json.loads("\n".join(data))
                        ev["_recv"] = time.monotonic()
                        handle(ev)
                    except Exception as e:   # one bad event must not kill the loop
                        log(f"event error: {type(e).__name__}: {e}")
                etype, data = "", []
            elif line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())


def main_loop(max_runs: int | None = None) -> None:
    backoff, runs = 1.0, 0
    while max_runs is None or runs < max_runs:
        runs += 1
        t0 = time.monotonic()
        try:
            run_once()
        except urllib.error.HTTPError as e:
            log(f"relay HTTP {e.code} (check token / device name)")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log(f"relay unreachable: {e}")
        except Exception as e:   # never die
            log(f"stream error: {type(e).__name__}: {e}")
        if time.monotonic() - t0 >= 10:
            backoff = 1.0
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def main() -> int:
    if not DEVICE:
        log("FATAL: CLAUDE_VOICE_DEVICE not set")
        return 2
    if TOKEN and not creds_ok(RELAY):
        log("FATAL: refusing to send CLAUDE_VOICE_TOKEN over plain http to a remote host (use https)")
        return 2
    main_loop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
