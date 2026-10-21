#!/usr/bin/env python3
"""claude-voice in-session self-update: move a detached plugin checkout to CLAUDE_VOICE_REF (tag or branch).

Claude Code executes hook scripts fresh from disk, so replacing the files upgrades a running session from the next
hook call on; only hooks.json changes need a new session. Never raises, always exits 0.

  self_update.py --session-start   SessionStart hook: stdin ignored, no stdout (it would become context), honours
                                   CLAUDE_VOICE_AUTO_UPDATE=0
  self_update.py update [--ref X]  explicit update (ignores AUTO_UPDATE), prints a short result line
  self_update.py version           describe + plugin.json version + detached|branch

Env: CLAUDE_VOICE_REF, CLAUDE_VOICE_AUTO_UPDATE, CLAUDE_VOICE_GH_TOKEN (private repo; passed as git config env,
never in argv/URL/logs), CLAUDE_VOICE_LOG_LEVEL / CLAUDE_VOICE_STATE_DIR (hook.log, as speak.py).
Gates: git work tree, detached HEAD (branch checkouts are never touched), clean tracked files, lock in .git.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser(os.environ.get("CLAUDE_VOICE_STATE_DIR", "~/.claude-voice"))
LEVELS = {"off": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}
LOG_MAX_BYTES = 256 * 1024
PLUGIN_DIR = os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOTAL_TIMEOUT_S = 60
LOCK_STALE_S = 120
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def log(level: str, event: str, **fields) -> None:
    try:
        if LEVELS[level] > LEVELS.get(os.environ.get("CLAUDE_VOICE_LOG_LEVEL", "").strip().lower(), 0):
            return
        os.makedirs(STATE_DIR, exist_ok=True)
        p = os.path.join(STATE_DIR, "hook.log")
        if os.path.exists(p) and os.path.getsize(p) > LOG_MAX_BYTES:
            os.replace(p, p + ".1")
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level, "event": event, **fields}
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


class Git:
    def __init__(self, d: str):
        self.d, self.deadline = d, time.monotonic() + TOTAL_TIMEOUT_S
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        tok = os.environ.get("CLAUDE_VOICE_GH_TOKEN")
        if tok:
            b64 = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
            self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                            GIT_CONFIG_VALUE_0=f"Authorization: Basic {b64}")

    def __call__(self, *args: str) -> tuple[int, str]:
        left = max(1.0, self.deadline - time.monotonic())
        try:
            p = subprocess.run(["git", "-C", self.d, *args], capture_output=True, text=True, timeout=left, env=self.env)
            return p.returncode, p.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""


def acquire_lock(path: str) -> bool:
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE_S:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            return False
        except OSError:
            return False
    return False


def describe(g: Git) -> str:
    rc, out = g("describe", "--tags", "--always")
    return out if rc == 0 and out else "unknown"


def update(ref: str | None, auto: bool) -> tuple[str, str]:
    """Returns (status, human message)."""
    if not ref:
        return "skip:no_ref", "no ref (set CLAUDE_VOICE_REF or pass a tag)"
    if not REF_RE.match(ref) or ".." in ref:
        return "skip:invalid_ref", "invalid ref"
    if auto and os.environ.get("CLAUDE_VOICE_AUTO_UPDATE", "") == "0":
        return "skip:auto_update_off", "CLAUDE_VOICE_AUTO_UPDATE=0"
    g = Git(PLUGIN_DIR)
    rc, out = g("rev-parse", "--is-inside-work-tree")
    if rc != 0 or out != "true":
        return "skip:not_git", "plugin dir is not a git checkout"
    if g("symbolic-ref", "-q", "HEAD")[0] == 0:
        return "skip:branch_checkout", "branch checkout (development), not touched"
    rc, out = g("status", "--porcelain", "--untracked-files=no")
    if rc != 0 or out:
        return "skip:dirty", "local modifications"
    rc, gitdir = g("rev-parse", "--absolute-git-dir")
    lock = os.path.join(gitdir, "claude-voice-update.lock")
    if rc != 0 or not acquire_lock(lock):
        return "skip:locked", "another update is running"
    try:
        head = g("rev-parse", "HEAD")[1]
        before = describe(g)
        rc, target = g("rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
        if rc != 0:
            if g("fetch", "--depth", "1", "--no-tags", "origin", f"refs/tags/{ref}:refs/tags/{ref}")[0] == 0:
                rc, target = g("rev-parse", "--verify", "-q", f"refs/tags/{ref}^{{commit}}")
            elif g("fetch", "--depth", "1", "origin", ref)[0] == 0:
                rc, target = g("rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}")
            else:
                return "skip:fetch_failed", "fetch failed (network? private repo without CLAUDE_VOICE_GH_TOKEN?)"
            if rc != 0:
                return "skip:fetch_failed", "ref not found"
        if target == head:
            return "noop:up_to_date", f"already at {before}"
        old_hooks = g("hash-object", "hooks/hooks.json")[1]
        if g("checkout", "-q", "--detach", target)[0] != 0:
            return "skip:checkout_failed", "checkout failed"
        msg = f"{before} -> {describe(g)} (in-session upgrade active from the next hook call)"
        if g("hash-object", "hooks/hooks.json")[1] != old_hooks:
            msg += "\nhooks.json changed: start a new session to apply hook registration changes"
            log("warn", "self_update", note="hooks.json changed")
        return "updated", msg
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def version() -> str:
    g = Git(PLUGIN_DIR)
    try:
        pv = json.load(open(os.path.join(PLUGIN_DIR, ".claude-plugin", "plugin.json"))).get("version", "?")
    except (OSError, ValueError):
        pv = "?"
    mode = "branch" if g("symbolic-ref", "-q", "HEAD")[0] == 0 else "detached"
    return f"{describe(g)} plugin.json={pv} {mode}"


def main(argv: list[str]) -> int:
    ref = os.environ.get("CLAUDE_VOICE_REF") or None
    if "--ref" in argv and argv.index("--ref") + 1 < len(argv):
        ref = argv[argv.index("--ref") + 1]
    if "--session-start" in argv:
        try:
            sys.stdin.read()
        except Exception:
            pass
        status, msg = update(ref, auto=True)
        log("info", "self_update", status=status, ref=ref, message=msg.splitlines()[0])
        return 0
    if argv[:1] == ["update"]:
        status, msg = update(ref, auto=False)
        log("info", "self_update", status=status, ref=ref, message=msg.splitlines()[0])
        print(f"claude-voice: {msg}" if status in ("updated", "noop:up_to_date") else f"claude-voice: skipped: {status[5:] if status.startswith('skip:') else status} ({msg})")
        return 0
    if argv[:1] == ["version"]:
        print(version())
        return 0
    print("usage: self_update.py --session-start | update [--ref X] | version")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        sys.exit(0)
