#!/usr/bin/env python3
"""claude-voice relay: receives speak requests, fans them out to devices/groups via SSE.

Stdlib only (Python >= 3.11). Auth: Bearer token(s) from CLAUDE_VOICE_TOKEN(S).
CLAUDE_VOICE_LOG_LEVEL=off|error|warn|info|debug (default info): info logs ids/devices/status/char counts only;
debug also logs message text. Tokens and Authorization headers are never logged. FATAL startup errors always print.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import sys
import threading
import time
import tomllib
import uuid
from types import SimpleNamespace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

VERSION = "0.1.6"
KEEPALIVE_S = 15
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
MAX_TRACKED = 1000
MAX_MUTED_SESSIONS = 1000
MAX_FIELD = 128
MAX_STREAMS = 64
ACK_STATUSES = {"delivered", "played", "tts_error", "player_error", "expired", "acked"}


LEVELS = {"off": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}
LOG_LEVEL = LEVELS["info"]


def parse_level(v: str | None, default: str) -> tuple[int, bool]:
    """Returns (level, valid). Empty/None is valid and means default."""
    v = (v or "").strip().lower()
    if not v:
        return LEVELS[default], True
    return (LEVELS[v], True) if v in LEVELS else (LEVELS[default], False)


def log(msg: str, level: str = "info") -> None:
    """level 'always' (FATAL / security warnings) ignores CLAUDE_VOICE_LOG_LEVEL."""
    if level == "always" or LEVELS[level] <= LOG_LEVEL:
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return host == "localhost"
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class State:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.devices: dict[str, dict] = cfg.get("devices", {})
        self.groups: dict[str, list[str]] = cfg.get("groups", {})
        self.routes: dict[str, str] = cfg.get("routes", {})
        d = cfg.get("defaults", {})
        self.default_route = d.get("route", "active")
        self.default_ttl = int(d.get("ttl_s", 45))
        self.queues: dict[str, queue.Queue] = {n: queue.Queue(maxsize=100) for n in self.devices}
        self.last_seen: dict[str, float] = {}
        self.connections: dict[str, int] = collections.defaultdict(int)
        self.muted: dict[str, set[str]] = {"all": set(), "session": set(), "device": set(), "group": set()}
        self.history: collections.deque = collections.deque(maxlen=200)
        self.by_id: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.session_order: collections.deque = collections.deque()
        self.gen: dict[str, int] = collections.defaultdict(int)

    # ---- resolution -------------------------------------------------
    def resolve(self, target: str) -> list[str]:
        with self.lock:
            return self._resolve(target)

    def _resolve(self, target: str) -> list[str]:
        if target == "all":
            return list(self.devices)
        if target == "active":
            live = [(t, n) for n, t in self.last_seen.items() if self.connections[n] > 0 and n in self.devices]
            return [max(live)[1]] if live else []
        if target in self.groups:
            return [d for d in self.groups[target] if d in self.devices]
        if target in self.devices:
            return [target]
        return []

    def is_muted(self, device: str, groups: list[str], session_id: str | None) -> str | None:
        with self.lock:
            return self._is_muted(device, groups, session_id)

    def _is_muted(self, device: str, groups: list[str], session_id: str | None) -> str | None:
        if "*" in self.muted["all"]:
            return "all"
        if session_id and session_id in self.muted["session"]:
            return "session"
        if device in self.muted["device"]:
            return "device"
        for g in groups:
            if g in self.muted["group"]:
                return f"group:{g}"
        return None

    # ---- enqueue ----------------------------------------------------
    def speak(self, body: dict) -> dict:
        with self.lock:
            return self._speak(body)

    @staticmethod
    def _opt_str(v) -> str | None:
        if v is None or v == "":
            return None
        v = str(v)
        if len(v) > MAX_FIELD:
            raise ValueError(f"field longer than {MAX_FIELD} chars")
        return v

    def _purge_expired(self, dev: str) -> None:
        q, now = self.queues[dev], time.time()
        with q.mutex:
            gone = [e for e in q.queue if e["_expires"] <= now]
            if not gone:
                return
            keep = [e for e in q.queue if e["_expires"] > now]
            q.queue.clear()
            q.queue.extend(keep)
            q.not_full.notify_all()
        for e in gone:
            rec = self.by_id.get(e["id"])
            if rec:
                rec["status"] = "expired"

    def requeue(self, dev: str, ev: dict) -> None:
        """Put an undelivered event back at the head without ever blocking; drop if the queue is full."""
        q = self.queues[dev]
        with self.lock, q.mutex:
            if len(q.queue) < q.maxsize:
                q.queue.appendleft(ev)
                q.not_empty.notify()
                return
        self.set_status(ev["id"], "dropped:queue_full")

    def set_status(self, evid: str, status: str) -> None:
        with self.lock:
            rec = self.by_id.get(evid)
            if rec:
                rec["status"] = status

    def _speak(self, body: dict) -> dict:
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("text required")
        text = text[:MAX_TEXT]
        source = self._opt_str(body.get("source"))
        target = self._opt_str(body.get("target")) or self.routes.get(source or "", self.default_route)
        session_id = self._opt_str(body.get("session_id"))
        priority = self._opt_str(body.get("priority")) or "normal"
        play_at = self._opt_str(body.get("play_at"))
        try:
            ttl = int(float(body.get("ttl_s") or self.default_ttl))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("ttl_s must be a number") from None
        ttl = max(1, min(ttl, 3600))
        now = time.time()
        devices = self.resolve(target)
        if not devices:
            raise LookupError("unknown target or no device connected")
        group_names = [g for g, m in self.groups.items() if g == target]
        result = []
        for dev in devices:
            groups_of = [g for g, m in self.groups.items() if dev in m] if target in self.groups else []
            muted = self.is_muted(dev, group_names or groups_of, session_id)
            cfg = self.devices.get(dev, {})
            ev = {
                "id": "evt_" + uuid.uuid4().hex[:12],
                "device": dev,
                "session_id": session_id,
                "target": target,
                "text": text,
                "priority": priority,
                "ttl_s": ttl,
                "play_at": play_at,
                "volume": cfg.get("volume", 1.0),
                "latency_offset_ms": cfg.get("latency_offset_ms", 0),
                "created": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "_expires": now + ttl,
            }
            rec = {"id": ev["id"], "device": dev, "target": target, "session_id": session_id,
                   "status": "muted:" + muted if muted else "queued", "ts": now}
            self.by_id[ev["id"]] = rec
            while len(self.by_id) > MAX_TRACKED:
                self.by_id.popitem(last=False)
            self.history.append(rec)
            if not muted:
                try:
                    self.queues[dev].put_nowait(ev)
                except queue.Full:
                    self._purge_expired(dev)
                    try:
                        self.queues[dev].put_nowait(ev)
                    except queue.Full:
                        rec["status"] = "dropped:queue_full"
            result.append({"id": ev["id"], "device": dev, "status": rec["status"]})
        return {"target": target, "events": result}

    def ack(self, evid: str, device: str, status: str = "acked") -> bool:
        with self.lock:
            rec = self.by_id.get(evid)
            if not rec or rec["device"] != device:
                return False
            rec["status"] = status
            return True

    def set_mute(self, scope: str, ident: str, muted: bool) -> None:
        with self.lock:
            self._set_mute(scope, ident, muted)

    def _set_mute(self, scope: str, ident: str, muted: bool) -> None:
        if scope not in self.muted:
            raise ValueError("scope must be all|session|device|group")
        if len(ident) > MAX_FIELD:
            raise ValueError(f"id longer than {MAX_FIELD} chars")
        if scope == "all":
            ident = "*"
        if scope == "device" and ident not in self.devices:
            raise ValueError(f"unknown device {ident!r}")
        if scope == "group" and ident not in self.groups:
            raise ValueError(f"unknown group {ident!r}")
        (self.muted[scope].add if muted else self.muted[scope].discard)(ident)
        if scope == "session" and muted:
            if ident in self.session_order:
                self.session_order.remove(ident)
            self.session_order.append(ident)
            while len(self.session_order) > MAX_MUTED_SESSIONS:
                self.muted["session"].discard(self.session_order.popleft())

    def muted_view(self) -> dict:
        with self.lock:
            return {k: sorted(v) for k, v in self.muted.items()}

    def touch(self, dev: str) -> None:
        with self.lock:
            self.last_seen[dev] = time.time()

    def attach(self, dev: str) -> int:
        with self.lock:
            self.gen[dev] += 1
            self.connections[dev] += 1
            self.last_seen[dev] = time.time()
            return self.gen[dev]

    def detach(self, dev: str) -> None:
        with self.lock:
            self.connections[dev] -= 1

    def superseded(self, dev: str, gen: int) -> bool:
        with self.lock:
            return self.gen[dev] != gen

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        now = time.time()
        return {
            "version": VERSION,
            "devices": {n: {"connected": self.connections[n] > 0,
                            "last_seen_s_ago": round(now - self.last_seen[n], 1) if n in self.last_seen else None,
                            "queued": self.queues[n].qsize(), **{k: v for k, v in c.items()}}
                        for n, c in self.devices.items()},
            "groups": self.groups,
            "routes": {"default": self.default_route, **self.routes},
            "muted": {k: sorted(v) for k, v in self.muted.items()},
            "recent": [{k: v for k, v in r.items() if k != "ts"} | {"age_s": round(now - r["ts"], 1)}
                       for r in list(self.history)[-20:]],
        }


def norm_base_path(v) -> str:
    v = str(v or "").strip().strip("/")
    return "/" + v if v else ""


def make_handler(state: State, tokens: list[str], no_auth: bool, base_path: str = "", max_streams: int = MAX_STREAMS):
    streams = threading.BoundedSemaphore(max_streams)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"claude-voice-relay/{VERSION}"
        timeout = 30   # socket timeout: half-open peers and slowloris

        def log_message(self, fmt, *args):  # never log headers/tokens
            msg = (fmt % args).encode("unicode_escape").decode("ascii")   # no control chars in logs
            if fmt.startswith("Request timed out"):   # idle keep-alive closed by the 30 s socket timeout
                return log(f"{self.client_address[0]} {msg}", "debug")
            try:
                bad = int(args[1]) >= 400
            except (IndexError, ValueError, TypeError):
                bad = True
            log(f"{self.client_address[0]} {msg}", "warn" if bad else "info")

        # -- helpers
        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if code >= 400:
                self.send_header("Connection", "close")
                self.close_connection = True   # request body may be unread
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if no_auth:
                return True
            hdr = self.headers.get("Authorization", "")
            if not hdr.startswith("Bearer "):
                return False
            given = hashlib.sha256(hdr[7:].strip().encode()).digest()
            ok = False
            for t in tokens:  # no early exit; equal-length digests
                ok |= hmac.compare_digest(given, hashlib.sha256(t.encode()).digest())
            return ok

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self.close_connection = True   # body unread: connection unusable
                raise ValueError("invalid or too large Content-Length")
            raw = self.rfile.read(n) if n else b"{}"
            obj = json.loads(raw or b"{}")
            if not isinstance(obj, dict):
                raise ValueError("JSON object expected")
            return obj

        def _guard(self) -> bool:
            if not self._authed():
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                return False
            return True

        # -- routes
        def _safe(self, fn) -> None:
            try:
                fn()
            except (OSError, ValueError) as e:
                if isinstance(e, OSError):
                    self.close_connection = True
                    return
                self._internal(e)
            except Exception as e:
                self._internal(e)

        def _internal(self, e: Exception) -> None:
            log(f"{self.client_address[0]} internal error: {type(e).__name__}: {e}", "error")
            self.close_connection = True
            if not self._streaming:
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        _streaming = False

        def do_GET(self):
            self._safe(self._get)

        def do_POST(self):
            self._safe(self._post)

        def _url(self):
            """Split the raw request target by hand; None if it must not be routed."""
            raw = self.path
            path, _, query = raw.partition("?")
            if not path.startswith("/") or path.startswith("//") or ";" in path:
                return None
            if base_path and (path == base_path or path.startswith(base_path + "/")):
                path = path[len(base_path):] or "/"
            return SimpleNamespace(path=path, query=query)

        def _bad_path(self) -> None:
            self._json(400, {"error": "bad request path"})

        def _get(self):
            u = self._url()
            if u is None:
                return self._bad_path()
            if u.path == "/healthz":
                return self._json(200, {"ok": True, "version": VERSION})
            if not self._guard():
                return
            if u.path == "/v1/state":
                return self._json(200, state.snapshot())
            if u.path == "/v1/stream":
                dev = (parse_qs(u.query).get("device") or [""])[0]
                if dev not in state.devices:
                    return self._json(404, {"error": "unknown device"})
                return self._stream(dev)
            self._json(404, {"error": "not found"})

        def _post(self):
            u = self._url()
            if u is None:
                return self._bad_path()
            if not self._guard():
                return
            try:
                body = self._body()
                if u.path == "/v1/speak":
                    log(f"speak body={json.dumps(body, ensure_ascii=False)}", "debug")
                    res = state.speak(body)
                    log(f"speak target={res['target']} chars={len(str(body.get('text', '')).strip())} "
                        + " ".join(f"{e['id']}->{e['device']}:{e['status']}" for e in res["events"]))
                    for e in res["events"]:
                        if e["status"].startswith("dropped"):
                            log(f"event {e['id']} for {e['device']} {e['status']}", "warn")
                    return self._json(202, res)
                if u.path == "/v1/ack":
                    status = str(body.get("status", "acked"))
                    if status not in ACK_STATUSES:
                        raise ValueError("unknown ack status")
                    ok = state.ack(str(body.get("id")), str(body.get("device")), status)
                    log(f"ack id={str(body.get('id'))[:40]} device={str(body.get('device'))[:40]} status={status} ok={ok}",
                        "warn" if status in ("tts_error", "player_error", "expired") or not ok else "info")
                    if ok:
                        state.touch(str(body.get("device")))
                    return self._json(200 if ok else 404, {"ok": ok})
                if u.path == "/v1/mute":
                    state.set_mute(str(body.get("scope", "")), str(body.get("id", "")), bool(body.get("muted", True)))
                    log(f"mute scope={str(body.get('scope'))[:20]} id={str(body.get('id', ''))[:40]} muted={bool(body.get('muted', True))}")
                    return self._json(200, {"muted": state.muted_view()})
            except LookupError as e:
                return self._json(404, {"error": str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def _stream(self, dev: str) -> None:
            if not streams.acquire(blocking=False):
                return self._json(503, {"error": "too many streams"})
            try:
                self._stream_locked(dev)
            finally:
                streams.release()

        def _stream_locked(self, dev: str) -> None:
            self._streaming = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            gen = state.attach(dev)   # newest connection per device wins
            log(f"device {dev} connected")
            q = state.queues[dev]
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                last_write = time.monotonic()
                while True:
                    if state.superseded(dev, gen):
                        log(f"device {dev} stream superseded by a newer connection", "warn")
                        break
                    try:
                        ev = q.get(timeout=1)
                    except queue.Empty:
                        if time.monotonic() - last_write >= KEEPALIVE_S:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_write = time.monotonic()
                        continue
                    if state.superseded(dev, gen):
                        state.requeue(dev, ev)
                        log(f"device {dev} stream superseded by a newer connection", "warn")
                        break
                    if time.time() > ev["_expires"]:
                        state.ack(ev["id"], dev, "expired")
                        log(f"event {ev['id']} expired before delivery to {dev}", "warn")
                        continue
                    out = {k: v for k, v in ev.items() if not k.startswith("_")}
                    out["expires_in_s"] = round(max(0.0, ev["_expires"] - time.time()), 3)
                    payload = f"id: {ev['id']}\nevent: speak\ndata: {json.dumps(out)}\n\n".encode()
                    try:
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except OSError:
                        state.requeue(dev, ev)  # not delivered: keep for reconnect (TTL still applies)
                        raise
                    last_write = time.monotonic()
                    state.ack(ev["id"], dev, "delivered")
                    state.touch(dev)
                    log(f"deliver {ev['id']} -> {dev} payload={json.dumps(out, ensure_ascii=False)}", "debug")
            except (OSError, ValueError):
                pass
            finally:
                state.detach(dev)
                log(f"device {dev} disconnected")
                self.close_connection = True

    return H


def load_config(path: str | None) -> dict:
    if not path:
        return {"devices": {"local": {}}, "groups": {}, "defaults": {"route": "active"}}
    with open(path, "rb") as f:
        return tomllib.load(f)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="claude-voice relay")
    ap.add_argument("--config", default=os.environ.get("CLAUDE_VOICE_CONFIG"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    global LOG_LEVEL
    LOG_LEVEL, valid = parse_level(os.environ.get("CLAUDE_VOICE_LOG_LEVEL"), "info")
    if not valid:
        log("WARNING: invalid CLAUDE_VOICE_LOG_LEVEL, using info", "always")
    if LOG_LEVEL == LEVELS["debug"]:
        log("WARNING: log level debug: message text is written to the log", "always")
    cfg = load_config(a.config)
    host = a.host or os.environ.get("CLAUDE_VOICE_HOST") or cfg.get("server", {}).get("host", "127.0.0.1")
    port = a.port or int(os.environ.get("CLAUDE_VOICE_PORT") or cfg.get("server", {}).get("port", 8787))

    tokens = [t.strip() for t in (os.environ.get("CLAUDE_VOICE_TOKENS") or os.environ.get("CLAUDE_VOICE_TOKEN") or "").split(",") if t.strip()]
    base_path = norm_base_path(os.environ.get("CLAUDE_VOICE_BASE_PATH") or cfg.get("server", {}).get("base_path"))
    no_auth = os.environ.get("LOCAL_UNSAFE_NO_AUTH", "").lower() in ("1", "true", "yes")

    if no_auth:
        if not is_loopback(host):
            log(f"FATAL: LOCAL_UNSAFE_NO_AUTH=true is only allowed on loopback (host={host!r})", "always")
            return 2
        log("WARNING: running WITHOUT authentication (loopback only)", "always")
    elif not tokens:
        log("FATAL: no CLAUDE_VOICE_TOKEN set. Set a token, or LOCAL_UNSAFE_NO_AUTH=true on 127.0.0.1.", "always")
        return 2
    elif any(len(t) < 16 for t in tokens):
        log("FATAL: token too short (min 16 chars)", "always")
        return 2

    state = State(cfg)
    srv = ThreadingHTTPServer((host, port), make_handler(state, tokens, no_auth, base_path))
    srv.daemon_threads = True
    log(f"claude-voice relay {VERSION} on {host}:{srv.server_address[1]} "
        f"devices={list(state.devices)} groups={list(state.groups)} base_path={base_path or '-'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
