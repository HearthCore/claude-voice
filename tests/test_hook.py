"""Hook tests: fd hygiene, last_assistant_message, edge cases."""
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

from common import ROOT, check, free_port
from test_relay import wait_for

sys.path.insert(0, os.path.join(ROOT, "hooks"))
import speak  # noqa: E402


def run_hook(payload, env, timeout=10):
    t = time.time()
    p = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=env, capture_output=True, timeout=timeout,
                       input=json.dumps(payload).encode())
    return p, time.time() - t


def run():
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
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{cap.server_address[1]}", "CLAUDE_VOICE_STATE_DIR": tmp}
    env.pop("CLAUDE_VOICE_SYNC", None)
    for k in ("CLAUDE_VOICE_MAX_CHARS", "CLAUDE_VOICE_TARGET"):
        env.pop(k, None)

    # black hole: accepts, never answers. Hook must release Claude Code's pipes at once.
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(16)
    held = []
    threading.Thread(target=lambda: [held.append(hole.accept()) for _ in iter(int, 1)], daemon=True).start()
    henv = {**env, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{hole.getsockname()[1]}", "CLAUDE_VOICE_TIMEOUT": "2", "CLAUDE_VOICE_RETRIES": "1"}
    p, dt = run_hook({"hook_event_name": "Notification", "message": "Hallo."}, henv)
    print(f"hook with captured pipes vs black-hole server: {dt:.2f}s")
    check("hook returns < 1s with captured pipes (fds released)", p.returncode == 0 and dt < 1.0)

    def sent(n, secs=3):
        return wait_for(lambda: len(posts) >= n, secs)

    p, _ = run_hook({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "Neu und fertig.",
                     "transcript_path": "/nonexistent"}, env)
    check("Stop uses last_assistant_message", sent(1) and posts[-1]["text"] == "Neu und fertig.")

    tr = os.path.join(tmp, "t.jsonl")
    open(tr, "w").write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Aus dem Transkript."}]}}) + "\n")
    run_hook({"hook_event_name": "Stop", "session_id": "s", "transcript_path": tr}, env)
    check("Stop falls back to transcript when field missing", sent(2) and posts[-1]["text"] == "Aus dem Transkript.")

    n = len(posts)
    run_hook({"hook_event_name": "Stop", "session_id": "s",
              "last_assistant_message": "```py\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"}, env)
    time.sleep(0.8)
    check("code block + table only -> nothing sent", len(posts) == n)

    long = "Das ist ein ganzer Satz. " * 100
    run_hook({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": long}, {**env, "CLAUDE_VOICE_MAX_CHARS": "120"})
    check("long answer cut at sentence boundary", sent(n + 1) and len(posts[-1]["text"]) <= 120 and posts[-1]["text"].endswith("Satz."))

    uni = "Grüße, Käse, Ünïcødé – schön."
    check("umlauts survive clean()", speak.clean("**" + uni + "**", 500) == uni)
    run_hook({"hook_event_name": "Notification", "message": uni}, env)
    check("umlauts arrive at relay", sent(n + 2) and posts[-1]["text"] == uni and posts[-1]["priority"] == "high")

    for bad in ("abc", "-5", ""):
        p, _ = run_hook({"hook_event_name": "Stop", "last_assistant_message": "Ok."}, {**env, "CLAUDE_VOICE_MAX_CHARS": bad})
        if bad == "abc":
            check("invalid CLAUDE_VOICE_MAX_CHARS -> exit 0", p.returncode == 0)
    p, _ = run_hook({"hook_event_name": "Stop", "last_assistant_message": "Ok."},
                    {**env, "CLAUDE_VOICE_TIMEOUT": "x", "CLAUDE_VOICE_RETRIES": "y"})
    check("invalid TIMEOUT/RETRIES -> exit 0", p.returncode == 0)
    p = subprocess.run([sys.executable, f"{ROOT}/hooks/speak.py"], env=env, input=b"\xff not json", capture_output=True, timeout=10)
    check("garbage stdin -> exit 0", p.returncode == 0)
    check("hooks.json entries are async", all(h["async"] is True and h["timeout"] == (90 if name == "SessionStart" else 10)
          for name, ev in json.load(open(f"{ROOT}/hooks/hooks.json"))["hooks"].items() for g in ev for h in g["hooks"]))
    # run every hooks.json command the way Claude Code does (shell, CLAUDE_PLUGIN_ROOT set) with `|| true` stripped:
    # the referenced script must exist and run (a quoting mistake would otherwise be masked by `|| true`)
    ok = []
    for name, ev in json.load(open(f"{ROOT}/hooks/hooks.json"))["hooks"].items():
        for g in ev:
            for h in g["hooks"]:
                cmd = h["command"].removesuffix(" || true")
                p = subprocess.run(["sh", "-c", cmd], input=json.dumps({"hook_event_name": name}).encode(), capture_output=True, timeout=30,
                                   env={**env, "CLAUDE_PLUGIN_ROOT": ROOT, "CLAUDE_VOICE_AUTO_UPDATE": "0"})
                ok.append((name, p.returncode, b"can't open file" not in p.stderr))
    check("every hooks.json command resolves to an existing script and exits 0", ok and all(rc == 0 and found for _, rc, found in ok))

    # log levels: off writes nothing; info = decision + metadata + count; debug adds content + payload + POST result
    def hook_log(level, payload):
        d = tempfile.mkdtemp()
        e = {**env, "CLAUDE_VOICE_STATE_DIR": d}
        if level:
            e["CLAUDE_VOICE_LOG_LEVEL"] = level
        run_hook(payload, e)
        time.sleep(0.5)
        f = os.path.join(d, "hook.log")
        return d, [json.loads(l) for l in open(f, encoding="utf-8")] if os.path.exists(f) else []

    stop_in = {"hook_event_name": "Stop", "session_id": "d", "prompt_id": "p1", "stop_reason": "end_turn",
               "agent_id": "a1", "last_assistant_message": "Zwischenstand: ich prüfe noch."}
    check("hook log level off (default) writes nothing", hook_log(None, stop_in)[1] == [] and hook_log("off", stop_in)[1] == [])
    d, recs = hook_log("info", stop_in)
    dec = [r for r in recs if r["event"] == "decision"]
    check("hook info: decision + metadata + count, no text", len(dec) == 1 and dec[0]["decision"] == "send" and dec[0]["input"]["prompt_id"] == "p1"
          and dec[0]["input"]["agent_id"] == "a1" and dec[0]["chars"] == len("Zwischenstand: ich prüfe noch.")
          and "last_assistant_message" in dec[0]["keys"] and "prüfe" not in open(os.path.join(d, "hook.log"), encoding="utf-8").read())
    check("hook.log is mode 0600", oct(os.stat(os.path.join(d, "hook.log")).st_mode & 0o777) == "0o600")
    d, recs = hook_log("debug", stop_in)
    cont = [r for r in recs if r["event"] == "content"]
    post_rec = [r for r in recs if r["event"] == "post"]
    check("hook debug: source, raw, cleaned text, payload and POST result",
          len(cont) == 1 and cont[0]["source"] == "last_assistant_message" and cont[0]["raw"].startswith("Zwischenstand")
          and cont[0]["text"] == "Zwischenstand: ich prüfe noch." and cont[0]["payload"]["text"] == cont[0]["text"]
          and len(post_rec) == 1 and post_rec[0]["result"] == "HTTP 202")
    _, recs = hook_log("debug", {"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "x"})
    check("hook debug records skip reasons", [r["decision"] for r in recs if r["event"] == "decision"] == ["skip:stop_hook_active"])
    _, recs = hook_log("bogus", stop_in)
    check("invalid hook log level -> off + one warning", [r["event"] for r in recs] == ["log"] and recs[0]["level"] == "warn")
    # rotation (white-box)
    rt = tempfile.mkdtemp()
    old = (speak.STATE_DIR, speak.LOG_MAX_BYTES)
    speak.STATE_DIR, speak.LOG_MAX_BYTES = rt, 500
    os.environ["CLAUDE_VOICE_LOG_LEVEL"] = "info"
    try:
        for i in range(20):
            speak.log("info", "x", pad="p" * 100)
    finally:
        speak.STATE_DIR, speak.LOG_MAX_BYTES = old
        os.environ.pop("CLAUDE_VOICE_LOG_LEVEL")
    check("hook.log rotates to hook.log.1 (one generation)", sorted(os.listdir(rt)) == ["hook.log", "hook.log.1"]
          and os.path.getsize(os.path.join(rt, "hook.log")) <= 700)
    # interim Stop while a background subagent runs
    sub = [{"id": "af73", "type": "subagent", "status": "running", "description": "recherche", "agent_type": "general-purpose"}]
    base_in = {"hook_event_name": "Stop", "session_id": "bg", "last_assistant_message": "Zwischenstand."}

    def delivered(extra, e=env):
        n0 = len(posts)
        p, _ = run_hook({**base_in, **extra}, e)
        time.sleep(0.8)
        return p.returncode == 0 and len(posts) > n0

    check("interim Stop with running subagent -> nothing sent", not delivered({"background_tasks": sub}))
    d, recs = hook_log("info", {**base_in, "background_tasks": sub})
    dec = [r for r in recs if r["event"] == "decision"]
    check("skip decision logged with subagent count", dec[0]["decision"] == "skip:background_subagent" and dec[0]["running_subagents"] == 1)
    _, recs = hook_log("debug", {**base_in, "background_tasks": sub})
    check("debug logs background_tasks list", any(r["event"] == "background_tasks" and r["tasks"][0]["id"] == "af73" for r in recs))
    check("STOP_MODE=all speaks anyway", delivered({"background_tasks": sub}, {**env, "CLAUDE_VOICE_STOP_MODE": "all"}))
    check("running shell task does not suppress", delivered({"background_tasks": [{"type": "shell", "status": "running"}]}))
    check("finished subagent does not suppress", delivered({"background_tasks": [{**sub[0], "status": "completed"}]}))
    check("empty background_tasks -> sent", delivered({"background_tasks": []}))
    check("missing background_tasks -> sent", delivered({}))
    check("garbage background_tasks (string/dict/mixed) -> sent, exit 0",
          all(delivered({"background_tasks": g}) for g in ("running", {"type": "subagent", "status": "running"}, [None, 3, "x"])))
    n0 = len(posts)
    run_hook({"hook_event_name": "Notification", "message": "Frage.", "background_tasks": sub}, env)
    check("Notification unaffected by running subagent", sent(n0 + 1))
    cap.shutdown()


def run_creds():
    got_a, got_b = [], []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got_b.append(self.headers.get("Authorization"))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    tgt = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Target)
    threading.Thread(target=tgt.serve_forever, daemon=True).start()

    class Redir(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got_a.append(self.headers.get("Authorization"))
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{tgt.server_address[1]}/v1/speak")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    red = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Redir)
    threading.Thread(target=red.serve_forever, daemon=True).start()
    env = {**os.environ, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{red.server_address[1]}", "CLAUDE_VOICE_TOKEN": "t" * 32,
           "CLAUDE_VOICE_SYNC": "1", "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp()}
    run_hook({"hook_event_name": "Notification", "message": "Hallo."}, env)
    check("hook: redirect target never sees Authorization", got_a == ["Bearer " + "t" * 32] and got_b == [])
    check("creds_ok rules", speak.creds_ok("https://x.example") and speak.creds_ok("http://127.0.0.5:1") and speak.creds_ok("http://[::1]:1")
          and speak.creds_ok("http://localhost") and not speak.creds_ok("http://10.0.0.1") and not speak.creds_ok("http://evil.example")
          and not speak.creds_ok("ftp://127.0.0.1"))
    p, dt = run_hook({"hook_event_name": "Notification", "message": "Hallo."},
                     {**env, "CLAUDE_VOICE_URL": "http://10.255.255.1:9", "CLAUDE_VOICE_TIMEOUT": "3"})
    check("hook: token + non-https remote -> silent no-op", p.returncode == 0 and dt < 1.0)
    red.shutdown()
    tgt.shutdown()


def run_voicectl_creds():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "CLAUDE_VOICE_TOKEN": "t" * 32, "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp()}
    p = subprocess.run([sys.executable, f"{ROOT}/tools/voicectl.py", "status"], capture_output=True, timeout=10,
                       env={**env, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{srv.server_address[1]}"})
    check("voicectl: 302 not followed (HTTP error, token sent once)", p.returncode != 0 and b"302" in p.stderr and len(seen) == 1)
    p = subprocess.run([sys.executable, f"{ROOT}/tools/voicectl.py", "status"], capture_output=True, timeout=10,
                       env={**env, "CLAUDE_VOICE_URL": "http://10.255.255.1:9"})
    check("voicectl: refuses token over plain http to remote host", p.returncode != 0 and b"refusing" in p.stderr and len(seen) == 1)
    srv.shutdown()


def run_round5():
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

    # --- ancestry walker on a fake /proc
    def tree(procs):
        root = tempfile.mkdtemp()
        for pid, (ppid, comm, cmd) in procs.items():
            d = os.path.join(root, str(pid))
            os.makedirs(d)
            open(os.path.join(d, "status"), "w").write(f"Name:\t{comm}\nPPid:\t{ppid}\n")
            open(os.path.join(d, "comm"), "w").write(comm + "\n")
            open(os.path.join(d, "cmdline"), "wb").write(b"\0".join(c.encode() for c in cmd) + b"\0")
        return root

    old = speak.PROC
    try:
        cases = {
            "no claude ancestor": ({10: (11, "bash", ["bash"]), 11: (1, "systemd", ["systemd"])}, False),
            "single claude behind a shell": ({9: (10, "sh", ["sh", "-c", "x"]), 10: (11, "claude", ["claude"]), 11: (1, "zsh", ["zsh"])}, False),
            "claude grandparent (comm)": ({10: (11, "claude", ["claude", "-p"]), 11: (12, "claude", ["claude"]), 12: (1, "zsh", ["zsh"])}, True),
            "claude behind sh, nested": ({9: (10, "sh", ["sh"]), 10: (11, "claude", ["claude"]), 11: (12, "bash", ["bash"]), 12: (13, "claude", ["claude"]), 13: (1, "zsh", ["zsh"])}, True),
            "node-based claude via cmdline": ({10: (11, "node", ["node", "/a/@anthropic-ai/claude-code/cli.js"]),
                                               11: (1, "node", ["node", "/b/node_modules/@anthropic-ai/claude-code/cli.js", "-p"])}, True),
            "argv0 basename claude": ({10: (11, "x", ["/usr/bin/claude"]), 11: (12, "y", ["/opt/bin/claude", "-p"]), 12: (1, "z", ["z"])}, True),
            "cycle": ({10: (11, "claude", ["claude"]), 11: (10, "claude", ["claude"])}, False),
            "broken status": ({10: (11, "claude", ["claude"])}, False),
        }
        for name, (procs, want) in cases.items():
            speak.PROC = tree(procs)
            if name == "broken status":
                open(os.path.join(speak.PROC, "10", "status"), "w").write("garbage\n")
            check(f"nested walker: {name} -> {want}", speak.nested_claude(10 if name != "single claude behind a shell" and name != "claude behind sh, nested" else 9) is want)
        speak.PROC = "/nonexistent-proc"
        check("nested walker: missing /proc -> False", speak.nested_claude(10) is False)
    finally:
        speak.PROC = old

    # --- end to end: two processes named claude above the hook
    tmp = tempfile.mkdtemp()
    claude = os.path.join(tmp, "claude")
    os.symlink(os.path.realpath(sys.executable), claude)
    inner = ("import subprocess,sys,os;subprocess.run([sys.executable,%r],input=os.environ['PAYLOAD'].encode())" % f"{ROOT}/hooks/speak.py")
    outer = "import subprocess,sys,os;subprocess.run([%r,'-c',%r])" % (claude, inner)
    base = {**os.environ, "CLAUDE_VOICE_URL": f"http://127.0.0.1:{cap.server_address[1]}", "CLAUDE_VOICE_LOG_LEVEL": "info",
            "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp(), "PAYLOAD": json.dumps({"hook_event_name": "Stop", "session_id": "n", "last_assistant_message": "Hilfssession."})}
    base.pop("CLAUDE_VOICE_NESTED", None)
    base.pop("CLAUDE_PID", None)
    subprocess.run([claude, "-c", outer], env=base, timeout=20)
    time.sleep(0.8)
    logtxt = open(os.path.join(base["CLAUDE_VOICE_STATE_DIR"], "hook.log"), encoding="utf-8").read()
    check("nested helper claude: nothing sent, skip:nested logged", posts == [] and "skip:nested" in logtxt and "Hilfssession" not in logtxt)
    subprocess.run([claude, "-c", outer], env={**base, "CLAUDE_VOICE_NESTED": "speak"}, timeout=20)
    check("CLAUDE_VOICE_NESTED=speak speaks anyway", wait_for(lambda: len(posts) == 1))

    # --- notification allowlist
    def notif(extra, types):
        n0 = len(posts)
        env = {**os.environ, "CLAUDE_VOICE_URL": base["CLAUDE_VOICE_URL"], "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp()}
        if types is not None:
            env["CLAUDE_VOICE_NOTIFY_TYPES"] = types
        run_hook({"hook_event_name": "Notification", "message": "Bitte bestaetigen.", **extra}, env)
        time.sleep(0.7)
        return len(posts) > n0

    d = speak.DEFAULT_NOTIFY_TYPES
    check("notify default: permission_prompt spoken", notif({"notification_type": "permission_prompt"}, d))
    check("notify default: idle_prompt skipped", not notif({"notification_type": "idle_prompt"}, d))
    check("notify default: missing type skipped", not notif({}, d))
    check("notify all: everything incl. missing type", notif({"notification_type": "idle_prompt"}, "all") and notif({}, "all"))
    check("notify none: nothing", not notif({"notification_type": "permission_prompt"}, "none"))
    check("notify custom list", notif({"notification_type": "idle_prompt"}, "idle_prompt, auth_success"))

    # --- tagged context blocks
    c = speak.clean("<openviking-context>\nGeheime Erinnerung. Mehr.\n</openviking-context> Das ist der Satz.", 500)
    check("hyphenated context blocks dropped incl. content", c == "Das ist der Satz.")
    c = speak.clean("<system-reminder foo=\"1\">weg</system-reminder>Bleibt. a < b > c und <b>fett</b>", 500)
    check("plain angle brackets / simple tags survive", c.startswith("Bleibt.") and "weg" not in c and "a" in c and "fett" in c)
    cap.shutdown()


def run_round7():
    posts = []

    class Cap(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append((json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers.get("Authorization")))
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    cap = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Cap)
    threading.Thread(target=cap.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{cap.server_address[1]}"
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_VOICE_URL", "CLAUDE_VOICE_TOKEN", "CLAUDE_VOICE_TARGET", "CLAUDE_PLUGIN_OPTION"))}
    pl = {"hook_event_name": "Stop", "session_id": "r7", "prompt_id": "p", "last_assistant_message": "Konfig."}

    def fire(extra, payload=pl):
        n0 = len(posts)
        run_hook(payload, {**clean_env, "CLAUDE_VOICE_STATE_DIR": tempfile.mkdtemp(), "CLAUDE_VOICE_SYNC": "1", **extra})
        return posts[n0:]

    got = fire({"CLAUDE_PLUGIN_OPTION_RELAY_URL": url, "CLAUDE_PLUGIN_OPTION_RELAY_TOKEN": "o" * 20, "CLAUDE_PLUGIN_OPTION_TARGET": "tv"})
    check("userConfig env fallback: url, token, target", len(got) == 1 and got[0][1] == "Bearer " + "o" * 20 and got[0][0]["target"] == "tv")
    got = fire({"CLAUDE_VOICE_URL": url, "CLAUDE_VOICE_TOKEN": "e" * 20, "CLAUDE_VOICE_TARGET": "box", "CLAUDE_PLUGIN_OPTION_RELAY_URL": "http://127.0.0.1:1",
                "CLAUDE_PLUGIN_OPTION_RELAY_TOKEN": "o" * 20, "CLAUDE_PLUGIN_OPTION_TARGET": "tv"})
    check("explicit env wins over userConfig", len(got) == 1 and got[0][1] == "Bearer " + "e" * 20 and got[0][0]["target"] == "box")
    check("no url anywhere -> no-op", fire({}) == [])
    d = tempfile.mkdtemp()
    base = {"CLAUDE_VOICE_URL": url, "CLAUDE_VOICE_STATE_DIR": d, "CLAUDE_VOICE_DEDUP": "1", "CLAUDE_VOICE_LOG_LEVEL": "info"}

    def two(payload_a, payload_b):
        n0 = len(posts)
        ps = [subprocess.Popen([sys.executable, f"{ROOT}/hooks/speak.py"], env={**clean_env, **base, "CLAUDE_VOICE_SYNC": "1"}, stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
        for p_, pay in zip(ps, (payload_a, payload_b)):
            p_.stdin.write(json.dumps(pay).encode())
            p_.stdin.close()
        for p_ in ps:
            p_.wait(timeout=10)
        return len(posts) - n0

    check("same event from two copies (concurrent) -> one POST", two(pl, pl) == 1)
    check("repeat within window -> still suppressed", two(pl, pl) == 0)
    check("different text -> two POSTs", two({**pl, "last_assistant_message": "Anderer Text A."}, {**pl, "last_assistant_message": "Anderer Text B."}) == 2)
    check("different session -> two POSTs", two({**pl, "last_assistant_message": "Gleich."}, {**pl, "last_assistant_message": "Gleich.", "session_id": "r8"}) == 2)
    check("duplicate decision logged", "skip:duplicate" in open(os.path.join(d, "hook.log"), encoding="utf-8").read())
    # FS error -> send anyway
    blocker = os.path.join(tempfile.mkdtemp(), "file")
    open(blocker, "w").close()
    n0 = len(posts)
    run_hook({**pl, "last_assistant_message": "Trotz Fehler."}, {**clean_env, "CLAUDE_VOICE_URL": url, "CLAUDE_VOICE_STATE_DIR": blocker, "CLAUDE_VOICE_SYNC": "1"})
    check("guard failure never loses speech", len(posts) == n0 + 1)
    cap.shutdown()
