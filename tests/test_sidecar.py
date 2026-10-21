"""Sidecar tests: player selection, retries, headers, stale events, relay restart, unicode."""
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

from common import ROOT, TOKEN, check, free_port, req, wait_up

sys.path.insert(0, os.path.join(ROOT, "sidecar"))
os.environ.pop("CLAUDE_VOICE_PLAYER_CMD", None)
import sidecar  # noqa: E402
from test_relay import wait_for  # noqa: E402

TEXT_UNI = "Grüße, Käse, Ünïcødé – 🚀"


def status_of(base, evid):
    for r in req(base, "GET", "/v1/state")[1]["recent"]:
        if r["id"] == evid:
            return r["status"]


def start_relay(cfg, port, env):
    p = subprocess.Popen([sys.executable, f"{ROOT}/relay/relay.py", "--config", cfg, "--port", str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_up(f"http://127.0.0.1:{port}")
    return p


def unit():
    which = sidecar.shutil.which
    try:
        sidecar.shutil.which = lambda n: "/usr/bin/" + n if n == "mpv" else None
        raw = sidecar.player_cmd("Audio/PCM; rate=24000", "mp3", 24000, 1)
        check("mpv raw pcm flags for audio/pcm (case/params ignored)", raw[0] == "mpv" and "--demuxer=rawaudio" in raw
              and "--demuxer-rawaudio-format=s16le" in raw and "--demuxer-rawaudio-rate=24000" in raw
              and "--demuxer-rawaudio-channels=1" in raw and raw[-1] == "-")
        check("audio/l16 -> raw", "--demuxer=rawaudio" in sidecar.player_cmd("audio/L16", "mp3"))
        check("no content type + TTS_FORMAT=pcm -> raw", "--demuxer=rawaudio" in sidecar.player_cmd(None, "pcm"))
        check("audio/mpeg -> container mode", "--demuxer=rawaudio" not in sidecar.player_cmd("audio/mpeg", "pcm"))
        check("no content type + mp3 -> container mode", "--demuxer=rawaudio" not in sidecar.player_cmd("", "mp3"))
        sidecar.shutil.which = lambda n: "/usr/bin/" + n if n == "ffplay" else None
        sidecar.ffplay_has_ch_layout.cache_clear()
        sidecar.ffplay_has_ch_layout = lambda: True
        ff = sidecar.player_cmd("audio/pcm", "pcm", 22050, 2)
        check("ffplay raw flags", ff[0] == "ffplay" and ff[ff.index("-f") + 1] == "s16le" and ff[ff.index("-ar") + 1] == "22050"
              and ff[ff.index("-ch_layout") + 1] == "stereo" and ff[-2:] == ["-i", "pipe:0"])
        sidecar.ffplay_has_ch_layout = lambda: False
        check("ffplay falls back to -ac", "-ac" in sidecar.player_cmd("audio/pcm", "pcm", 24000, 1))
        os.environ["CLAUDE_VOICE_PLAYER_CMD"] = "cat -u"
        check("PLAYER_CMD overrides selection", sidecar.player_cmd("audio/pcm") == ["cat", "-u"])
    finally:
        sidecar.shutil.which = which
        os.environ.pop("CLAUDE_VOICE_PLAYER_CMD", None)

    h = sidecar.parse_extra_headers("X-A: 1; Bad Name: x; X-B: two:colon;Evil: a\rb; noColon; ")
    check("extra headers parsed, invalid names/CRLF values dropped", h == {"X-A": "1", "X-B": "two:colon"})

    acks, spoken = [], []
    old = (sidecar.ack, sidecar.speak)
    sidecar.ack = lambda i, s: acks.append((i, s))
    sidecar.speak = lambda ev: spoken.append(ev) or "played"
    try:
        from datetime import datetime, timedelta, timezone
        old_t = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        sidecar.handle({"id": "e1", "text": "alt", "created": old_t, "ttl_s": 45})
        sidecar.handle({"id": "e2", "text": "frisch", "created": datetime.now(timezone.utc).isoformat(), "ttl_s": 45})
        naive_old = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
        sidecar.handle({"id": "e3", "text": "naiv", "created": naive_old, "ttl_s": 45})
    finally:
        sidecar.ack, sidecar.speak = old
    check("stale event acked expired, not spoken", acks[0] == ("e1", "expired") and [e["id"] for e in spoken] == ["e2"]
          and acks[1] == ("e2", "played"))
    check("tz-naive created treated as UTC", acks[2] == ("e3", "expired"))

    # main loop survives any exception and backs off
    calls, sleeps = [], []
    old = (sidecar.run_once, sidecar.time.sleep)
    def boom():
        calls.append(1)
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    sidecar.run_once, sidecar.time.sleep = boom, sleeps.append
    try:
        sidecar.main_loop(max_runs=4)
    finally:
        sidecar.run_once, sidecar.time.sleep = old
    check("main loop survives UnicodeDecodeError, doubling backoff", len(calls) == 4 and sleeps == [1.0, 2.0, 4.0, 8.0])


def unit2():
    from datetime import datetime, timedelta, timezone
    acks, spoken = [], []
    old = (sidecar.ack, sidecar.speak)
    sidecar.ack = lambda i, s: acks.append((i, s))
    sidecar.speak = lambda ev: spoken.append(ev) or "played"
    try:
        far = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        now = time.monotonic()
        sidecar.handle({"id": "f1", "text": "x", "created": far, "ttl_s": 1, "expires_in_s": 30, "_recv": now})
        sidecar.handle({"id": "f2", "text": "x", "created": past, "ttl_s": 1, "expires_in_s": 30, "_recv": now})
        sidecar.handle({"id": "f3", "text": "x", "expires_in_s": 1.0, "_recv": now - 5})
    finally:
        sidecar.ack, sidecar.speak = old
    check("expires_in_s beats wall-clock created/ttl", acks == [("f1", "played"), ("f2", "played"), ("f3", "expired")])

    h = sidecar.parse_extra_headers("Authorization: x; host: y; Content-Type: z; Connection: c; Content-Length: 1; "
                                    "Transfer-Encoding: chunked; X-Ok: fine; X-Uni: gr\u00fc\u00df")
    check("extra header denylist + printable ASCII only", h == {"X-Ok": "fine"})
    p = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import sidecar" % os.path.join(ROOT, "sidecar")],
                       env={**os.environ, "TTS_PCM_RATE": "abc", "TTS_PCM_CHANNELS": "", "TTS_RETRIES": "x"}, capture_output=True, timeout=10)
    check("invalid numeric env -> defaults, no crash at import", p.returncode == 0)
    p = subprocess.run([sys.executable, f"{ROOT}/sidecar/sidecar.py"], capture_output=True, timeout=10,
                       env={**os.environ, "CLAUDE_VOICE_DEVICE": "d", "CLAUDE_VOICE_TOKEN": "t" * 32, "CLAUDE_VOICE_URL": "http://10.255.255.1:9"})
    check("sidecar refuses token over plain http to remote relay (exit 2)", p.returncode == 2 and b"FATAL" in p.stdout)

    # TTS credentials: remote plain http refused; redirects never followed
    got = []

    class Tgt(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    tgt = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Tgt)
    threading.Thread(target=tgt.serve_forever, daemon=True).start()

    class Red(Tgt):
        def do_POST(self):
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{tgt.server_address[1]}/v1/audio/speech")
            self.send_header("Content-Length", "0")
            self.end_headers()

    red = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Red)
    threading.Thread(target=red.serve_forever, daemon=True).start()
    saved = (sidecar.TTS_URL, sidecar.TTS_KEY)
    try:
        sidecar.TTS_KEY = "secret"
        sidecar.TTS_URL = "http://10.255.255.1:9/v1"
        t0 = time.time()
        try:
            sidecar.tts_open("x")
            ok = False
        except sidecar.TTSError:
            ok = time.time() - t0 < 1
        check("TTS key not sent over plain http to remote host", ok)
        sidecar.TTS_URL = f"http://127.0.0.1:{red.server_address[1]}/v1"
        try:
            sidecar.tts_open("x")
            ok = False
        except sidecar.TTSError:
            ok = True
        check("TTS redirect not followed (no Authorization at target)", ok and got == [])
    finally:
        sidecar.TTS_URL, sidecar.TTS_KEY = saved
        red.shutdown()
        tgt.shutdown()


def run():
    unit()
    unit2()
    tmp = tempfile.mkdtemp()
    cfg = os.path.join(tmp, "relay.toml")
    open(cfg, "w").write("[defaults]\nroute = \"d\"\n[devices.d]\n")
    seen, fake = [], {"mode": "ok", "n": 0, "ct": "audio/pcm"}

    class FakeTTS(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake["n"] += 1
            seen.append((body, dict(self.headers)))
            if fake["mode"] == "trunc":
                self.send_response(200)
                self.send_header("Content-Type", "audio/pcm")
                self.send_header("Content-Length", "1000")
                self.end_headers()
                self.wfile.write(b"\x00" * 100)
                self.close_connection = True
                return
            if fake["mode"] == "500" or (fake["mode"] == "502once" and fake["n"] == 1):
                self.send_response(500 if fake["mode"] == "500" else 502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", fake["cts"][fake["n"] - 1] if fake.get("cts") else fake["ct"])
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(fake.get("chunks", 3)):
                    c = b"\x00" * 100
                    self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass

        def log_message(self, *a):
            pass

    tts = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), FakeTTS)
    threading.Thread(target=tts.serve_forever, daemon=True).start()
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CLAUDE_VOICE_TOKEN": TOKEN}
    env.pop("LOCAL_UNSAFE_NO_AUTH", None)
    relay = start_relay(cfg, port, env)
    sink = os.path.join(tmp, "sink")
    senv = {**env, "CLAUDE_VOICE_URL": base, "CLAUDE_VOICE_DEVICE": "d", "TTS_URL": f"http://127.0.0.1:{tts.server_address[1]}/v1",
            "CLAUDE_VOICE_PLAYER_CMD": f"tee {sink}", "TTS_RETRIES": "2",
            "TTS_EXTRA_HEADERS": "P-Access-Token-Id: tid; P-Access-Token: tsecret; Bad Name: x; X-Inj: a\r\nInjected: 1"}
    sc = subprocess.Popen([sys.executable, f"{ROOT}/sidecar/sidecar.py"], env=senv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        check("sidecar connected to relay", wait_for(lambda: req(base, "GET", "/v1/state")[1]["devices"]["d"]["connected"], 5))

        def say(text, **kw):
            code, res = req(base, "POST", "/v1/speak", {"text": text, "target": "d", **kw})
            return res["events"][0]["id"]

        fake.update(mode="ok", n=0)
        e = say(TEXT_UNI)
        check("unicode text arrives intact at TTS", wait_for(lambda: status_of(base, e) == "played") and seen[-1][0]["input"] == TEXT_UNI)
        hdrs = {k.lower(): v for k, v in seen[-1][1].items()}
        check("TTS_EXTRA_HEADERS reach the TTS server", hdrs.get("p-access-token-id") == "tid" and hdrs.get("p-access-token") == "tsecret")
        check("CRLF/invalid extra headers rejected", "injected" not in hdrs and "x-inj" not in hdrs and "bad name" not in hdrs)
        check("default response_format is pcm", seen[-1][0]["response_format"] == "pcm")
        check("audio reached the player", os.path.exists(sink) and os.path.getsize(sink) == 300)

        fake.update(mode="502once", n=0)
        e = say("Ein Satz.")
        check("502 once then 200 -> retried and played", wait_for(lambda: status_of(base, e) == "played") and fake["n"] == 2)

        fake.update(mode="500", n=0)
        e = say("Ein Satz.")
        check("always 500 -> tts_error after retries", wait_for(lambda: status_of(base, e) == "tts_error", 8) and fake["n"] == 3)

        fake.update(mode="trunc", n=0)
        e = say("Abgeschnitten.")
        ok = wait_for(lambda: status_of(base, e) == "tts_error")
        check("truncated audio -> tts_error, never played", ok)

        fake.update(mode="ok", n=0, cts=["audio/pcm", "audio/mpeg"])
        e = say("Erster Satz. Zweiter Satz.")
        check("sentence with different content-type is skipped", wait_for(lambda: status_of(base, e) == "played")
              and wait_for(lambda: os.path.getsize(sink) == 300) and fake["n"] == 2)
        fake["cts"] = None

        # player dies mid-stream: no hang, fetch thread exits
        fake.update(mode="ok", n=0, chunks=40000)
        sidecar.TTS_URL = f"http://127.0.0.1:{tts.server_address[1]}/v1"
        os.environ["CLAUDE_VOICE_PLAYER_CMD"] = "true"
        before = threading.active_count()
        t0 = time.time()
        st = sidecar.speak({"text": "Lang."})
        os.environ.pop("CLAUDE_VOICE_PLAYER_CMD")
        check("dead player -> player_error, fetch thread released", st == "player_error" and time.time() - t0 < 10
              and wait_for(lambda: threading.active_count() <= before, 5))
        fake["chunks"] = 3

        # stalled player (never reads stdin): watchdog kills it, next event still works
        fake.update(mode="ok", n=0, chunks=3000)
        sidecar.PLAYER_DEADLINE_S = 2
        os.environ["CLAUDE_VOICE_PLAYER_CMD"] = "sleep 600"
        t0 = time.time()
        st = sidecar.speak({"text": "Haengt."})
        check("stalled player killed by deadline -> player_error", st == "player_error" and time.time() - t0 < 8)
        fake["chunks"] = 3
        os.environ["CLAUDE_VOICE_PLAYER_CMD"] = "tee /dev/null"
        check("sidecar works after a stalled player", sidecar.speak({"text": "Wieder."}) == "played")
        os.environ.pop("CLAUDE_VOICE_PLAYER_CMD")
        sidecar.PLAYER_DEADLINE_S = 180

        # relay restart: sidecar must reconnect on its own
        relay.send_signal(signal.SIGKILL)
        relay.wait()
        relay = start_relay(cfg, port, env)
        check("sidecar reconnects after relay restart", wait_for(lambda: req(base, "GET", "/v1/state")[1]["devices"]["d"]["connected"], 6))
        e = say("Nach Neustart.")
        check("speaks after reconnect", wait_for(lambda: status_of(base, e) == "played"))
    finally:
        for p in (sc, relay):
            p.send_signal(signal.SIGTERM)
        for p in (sc, relay):
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        tts.shutdown()
