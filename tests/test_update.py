"""In-session self-update tests against a local bare origin (no network)."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from common import ROOT, check, free_port
from test_relay import wait_for

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_TERMINAL_PROMPT": "0"}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, *a], env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


def build_origin():
    tmp = tempfile.mkdtemp()
    work = os.path.join(tmp, "work")
    os.makedirs(f"{work}/hooks")
    os.makedirs(f"{work}/.claude-plugin")
    for f in ("speak.py", "self_update.py", "hooks.json"):
        shutil.copy(f"{ROOT}/hooks/{f}", f"{work}/hooks/{f}")
    shutil.copy(f"{ROOT}/.claude-plugin/plugin.json", f"{work}/.claude-plugin/plugin.json")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "v1")
    git(work, "tag", "v1")
    sp = open(f"{work}/hooks/speak.py").read()
    assert '"source": "claude-code"' in sp
    open(f"{work}/hooks/speak.py", "w").write(sp.replace('"source": "claude-code"', '"source": "claude-code-v2"'))
    git(work, "commit", "-qam", "v2")
    git(work, "tag", "v2")
    h = json.load(open(f"{work}/hooks/hooks.json"))
    h["description"] = "v3"
    open(f"{work}/hooks/hooks.json", "w").write(json.dumps(h, indent=2) + "\n")
    git(work, "commit", "-qam", "v3")
    git(work, "tag", "v3")
    bare = os.path.join(tmp, "origin.git")
    subprocess.run(["git", "clone", "-q", "--bare", work, bare], check=True, env=GIT_ENV)
    return tmp, bare


def run():
    tmp, bare = build_origin()
    url = "file://" + bare
    posts = []

    class Cap(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    cap = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Cap)
    threading.Thread(target=cap.serve_forever, daemon=True).start()
    n = [0]

    def clone(tag="v1"):
        n[0] += 1
        d = os.path.join(tmp, f"clone{n[0]}")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", tag, url, d], check=True, env=GIT_ENV, capture_output=True)
        return d

    def sh(d, *args, ref=None, extra=None):
        state = tempfile.mkdtemp()
        env = {**GIT_ENV, "CLAUDE_VOICE_LOG_LEVEL": "info", "CLAUDE_VOICE_STATE_DIR": state}
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        env.pop("CLAUDE_VOICE_REF", None)
        if ref:
            env["CLAUDE_VOICE_REF"] = ref
        env.update(extra or {})
        p = subprocess.run([sys.executable, f"{d}/hooks/self_update.py", *args], env=env, capture_output=True, text=True,
                           input="{}", timeout=90)
        lf = os.path.join(state, "hook.log")
        st = [json.loads(l).get("status") for l in open(lf)] if os.path.exists(lf) else []
        return p, st

    def head(d):
        return git(d, "describe", "--tags", "--always")

    d = clone()
    p, st = sh(d, "--session-start", ref="v2")
    check("SessionStart updates to v2 silently", p.returncode == 0 and p.stdout == "" and head(d) == "v2" and st == ["updated"])
    git(d, "remote", "set-url", "origin", "file:///nonexistent/origin.git")
    p, st = sh(d, "--session-start", ref="v2")
    check("repeat is a no-op without network", p.returncode == 0 and st == ["noop:up_to_date"] and head(d) == "v2")
    git(d, "remote", "set-url", "origin", url)

    # e2e: next hook call runs the new code
    def hook_source(dd):
        n0 = len(posts)
        subprocess.run([sys.executable, f"{dd}/hooks/speak.py"], env={**os.environ, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{cap.server_address[1]}",
                       "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp(), "CLAUDE_VOICE_SYNC": "1"}, timeout=10,
                       input=json.dumps({"hook_event_name": "Stop", "session_id": "u", "last_assistant_message": "Hallo."}).encode())
        return posts[-1]["source"] if len(posts) > n0 else None

    d2 = clone("v1")
    before = hook_source(d2)
    sh(d2, "--session-start", ref="v2")
    check("hook after in-session update runs the new code", before == "claude-code" and hook_source(d2) == "claude-code-v2")

    p, st = sh(d, "update", ref="v3")
    check("v3 update announces hooks.json change", "v2 -> v3" in p.stdout and "hooks.json changed" in p.stdout and head(d) == "v3")

    d = clone()
    git(d, "checkout", "-q", "-b", "dev")
    p, st = sh(d, "--session-start", ref="v2")
    check("branch checkout is never touched", st == ["skip:branch_checkout"] and git(d, "rev-parse", "--abbrev-ref", "HEAD") == "dev")
    p, _ = sh(d, "update", ref="v2")
    check("branch skip names the real branch", "'dev'" in p.stdout and "development" not in p.stdout)

    # checkout created by the setup script on a branch (older setup without ref): marker makes it updatable
    d = clone()
    git(d, "checkout", "-q", "-b", "main")
    open(os.path.join(d, ".git", "claude-voice-managed"), "w").close()
    p, st = sh(d, "--session-start", ref="v2")
    check("managed branch checkout is updated (and detached)", st == ["updated"] and head(d) == "v2"
          and git(d, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD")

    # plugin dir inside another repo but not a checkout itself: never walk up into the parent repo
    outer = os.path.join(tmp, "outer")
    subprocess.run(["git", "init", "-q", "-b", "development", outer], check=True, env=GIT_ENV)
    inner = os.path.join(outer, "plugin")
    shutil.copytree(clone(), inner, ignore=shutil.ignore_patterns(".git"))
    p, st = sh(inner, "--session-start", ref="v2")
    check("plugin dir not the root of its own checkout -> skip:not_own_repo", st == ["skip:not_own_repo"])
    p, _ = sh(inner, "update", extra={"CLAUDE_PLUGIN_ROOT": clone()}, ref="v2")
    check("CLAUDE_PLUGIN_ROOT of another dir is ignored", "not the root of its own git checkout" in p.stdout)

    # setup script: ref from first argument, detached checkout, managed marker
    home = tempfile.mkdtemp()
    p = subprocess.run(["bash", f"{ROOT}/deploy/cloud-env-setup.sh", "v2"], capture_output=True, text=True, timeout=120,
                       env={"PATH": os.environ["PATH"], "HOME": home, "CLAUDE_VOICE_REPO": url, **{k: GIT_ENV[k] for k in GIT_ENV if k.startswith("GIT_")}})
    sd = os.path.join(home, ".claude-plugins", "claude-voice")
    check("setup script: arg ref, detached, managed marker", p.returncode == 0 and head(sd) == "v2"
          and git(sd, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD" and os.path.exists(os.path.join(sd, ".git", "claude-voice-managed")))
    p = subprocess.run(["bash", f"{ROOT}/deploy/cloud-env-setup.sh", "-x"], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "HOME": tempfile.mkdtemp()})
    check("setup script: invalid ref -> skipped, exit 0", p.returncode == 0 and "invalid ref" in p.stdout)

    d = clone()
    open(f"{d}/hooks/speak.py", "a").write("# local edit\n")
    p, st = sh(d, "--session-start", ref="v2")
    check("dirty tree is skipped", st == ["skip:dirty"] and head(d) == "v1")

    d = clone()
    for bad in ("-x", "a..b", "x y", ""):
        p, st = sh(d, "update", ref=bad or None)
        ok = p.returncode == 0 and head(d) == "v1"
    check("invalid refs skipped, exit 0", ok and sh(d, "update", ref="-x")[1] == ["skip:invalid_ref"])

    p, st = sh(d, "--session-start", ref="v2", extra={"CLAUDE_VOICE_AUTO_UPDATE": "0"})
    check("AUTO_UPDATE=0 skips SessionStart", st == ["skip:auto_update_off"] and head(d) == "v1")
    p, st = sh(d, "update", ref="v2", extra={"CLAUDE_VOICE_AUTO_UPDATE": "0"})
    check("explicit update ignores AUTO_UPDATE", head(d) == "v2" and "v1 -> v2" in p.stdout)

    d = clone()
    git(d, "remote", "set-url", "origin", "file:///nonexistent/origin.git")
    t0 = time.time()
    p, st = sh(d, "update", ref="v2")
    check("unreachable origin: exit 0, unchanged", p.returncode == 0 and head(d) == "v1" and st == ["skip:fetch_failed"] and "skipped" in p.stdout
          and time.time() - t0 < 30)
    git(d, "remote", "set-url", "origin", url)

    lock = os.path.join(d, ".git", "claude-voice-update.lock")
    open(lock, "w").close()
    p, st = sh(d, "update", ref="v2")
    check("fresh lock -> skip:locked", st == ["skip:locked"] and head(d) == "v1" and os.path.exists(lock))
    old = time.time() - 300
    os.utime(lock, (old, old))
    p, st = sh(d, "update", ref="v2")
    check("stale lock is taken over and removed", head(d) == "v2" and not os.path.exists(lock))

    p, _ = sh(d, "version")
    check("version output", "v2" in p.stdout and "plugin.json=" in p.stdout and "detached" in p.stdout)
    git(d, "checkout", "-q", "-b", "x")
    check("version reports branch", "branch" in sh(d, "version")[0].stdout)
    p, st = sh(d, "update")
    check("no ref -> skip:no_ref", st == ["skip:no_ref"])

    hj = json.load(open(f"{ROOT}/hooks/hooks.json"))["hooks"]
    cmds = [h["command"] for ev in hj.values() for g in ev for h in g["hooks"]]
    check("every hooks.json command ends with '|| true'", cmds and all(c.endswith(" || true") for c in cmds))
    ss = hj["SessionStart"][0]
    check("SessionStart registers self_update (async, timeout 90)", ss["matcher"] == "startup|resume"
          and 'self_update.py" --session-start' in ss["hooks"][0]["command"] and ss["hooks"][0]["async"] is True and ss["hooks"][0]["timeout"] == 90)
    cap.shutdown()
