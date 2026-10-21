#!/usr/bin/env python3
"""claude-voice relay: receives speak requests, fans them out to devices/groups via SSE.

Stdlib only (Python >= 3.11). Auth: Bearer token(s) from CLAUDE_VOICE_TOKEN(S).
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
KEEPALIVE_S = 15
MAX_BODY = 64 * 1024
MAX_TEXT = 4000
MAX_TRACKED = 1000


def log(msg: str) -> None:
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
        return None if v is None or v == "" else str(v)

    def _speak(self, body: dict) -> dict:
        text = str(body.get("text", "")).strip()
        if not text:
            raise ValueError("text required")
        text = text[:MAX_TEXT]
        source = self._opt_str(body.get("source"))
        target = str(body.get("target") or self.routes.get(source or "", self.default_route))
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
            raise LookupError(f"target {target!r} resolves to no device (unknown or none connected)")
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
        if scope == "all":
            ident = "*"
        if scope == "device" and ident not in self.devices:
            raise ValueError(f"unknown device {ident!r}")
        if scope == "group" and ident not in self.groups:
            raise ValueError(f"unknown group {ident!r}")
        (self.muted[scope].add if muted else self.muted[scope].discard)(ident)

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


def make_handler(state: State, tokens: list[str], no_auth: bool, base_path: str = ""):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"claude-voice-relay/{VERSION}"
        timeout = 30   # socket timeout: half-open peers and slowloris

        def log_message(self, fmt, *args):  # never log headers/tokens
            log(f"{self.client_address[0]} {fmt % args}")

        # -- helpers
        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
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
            log(f"{self.client_address[0]} internal error: {type(e).__name__}: {e}")
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
            u = urlparse(self.path)
            p = u.path
            if base_path and (p == base_path or p.startswith(base_path + "/")):
                p = p[len(base_path):] or "/"
            return u._replace(path=p)

        def _get(self):
            u = self._url()
            if u.path == "/healthz":
                return self._json(200, {"ok": True, "version": VERSION})
            if not self._guard():
                return
            if u.path == "/v1/state":
                return self._json(200, state.snapshot())
            if u.path == "/v1/stream":
                dev = (parse_qs(u.query).get("device") or [""])[0]
                if dev not in state.devices:
                    return self._json(404, {"error": f"unknown device {dev!r}"})
                return self._stream(dev)
            self._json(404, {"error": "not found"})

        def _post(self):
            if not self._guard():
                return
            u = self._url()
            try:
                body = self._body()
                if u.path == "/v1/speak":
                    return self._json(202, state.speak(body))
                if u.path == "/v1/ack":
                    ok = state.ack(str(body.get("id")), str(body.get("device")), str(body.get("status", "acked")))
                    return self._json(200 if ok else 404, {"ok": ok})
                if u.path == "/v1/mute":
                    state.set_mute(str(body.get("scope", "")), str(body.get("id", "")), bool(body.get("muted", True)))
                    return self._json(200, {"muted": state.muted_view()})
            except LookupError as e:
                return self._json(404, {"error": str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def _stream(self, dev: str) -> None:
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
                while not state.superseded(dev, gen):
                    try:
                        ev = q.get(timeout=1)
                    except queue.Empty:
                        state.touch(dev)
                        if time.monotonic() - last_write >= KEEPALIVE_S:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_write = time.monotonic()
                        continue
                    if state.superseded(dev, gen):
                        q.put(ev)
                        break
                    if time.time() > ev["_expires"]:
                        state.ack(ev["id"], dev, "expired")
                        continue
                    out = {k: v for k, v in ev.items() if not k.startswith("_")}
                    payload = f"id: {ev['id']}\nevent: speak\ndata: {json.dumps(out)}\n\n".encode()
                    try:
                        self.wfile.write(payload)
                        self.wfile.flush()
                    except OSError:
                        q.put(ev)  # not delivered: keep for reconnect (TTL still applies)
                        raise
                    last_write = time.monotonic()
                    state.ack(ev["id"], dev, "delivered")
                    state.touch(dev)
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

    cfg = load_config(a.config)
    host = a.host or os.environ.get("CLAUDE_VOICE_HOST") or cfg.get("server", {}).get("host", "127.0.0.1")
    port = a.port or int(os.environ.get("CLAUDE_VOICE_PORT") or cfg.get("server", {}).get("port", 8787))

    tokens = [t.strip() for t in (os.environ.get("CLAUDE_VOICE_TOKENS") or os.environ.get("CLAUDE_VOICE_TOKEN") or "").split(",") if t.strip()]
    base_path = norm_base_path(os.environ.get("CLAUDE_VOICE_BASE_PATH") or cfg.get("server", {}).get("base_path"))
    no_auth = os.environ.get("LOCAL_UNSAFE_NO_AUTH", "").lower() in ("1", "true", "yes")

    if no_auth:
        if not is_loopback(host):
            log(f"FATAL: LOCAL_UNSAFE_NO_AUTH=true is only allowed on loopback (host={host!r})")
            return 2
        log("WARNING: running WITHOUT authentication (loopback only)")
    elif not tokens:
        log("FATAL: no CLAUDE_VOICE_TOKEN set. Set a token, or LOCAL_UNSAFE_NO_AUTH=true on 127.0.0.1.")
        return 2
    elif any(len(t) < 16 for t in tokens):
        log("FATAL: token too short (min 16 chars)")
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
