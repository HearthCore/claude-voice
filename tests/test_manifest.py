"""Manifest checks and (if available) `claude plugin validate`."""
import os
import json
import re
import shutil
import subprocess

from common import ROOT, check


def run():
    pj = json.load(open(f"{ROOT}/.claude-plugin/plugin.json"))
    mj = json.load(open(f"{ROOT}/.claude-plugin/marketplace.json"))
    check("plugin name is not reserved (no claude- prefix)", pj["name"] == "voice-relay" and not pj["name"].startswith("claude-"))
    uc = pj["userConfig"]
    check("userConfig keys valid, fields complete", all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and {"type", "title", "description"} <= set(v)
          for k, v in uc.items()) and not any(v.get("required") for v in uc.values()))
    check("only the token is sensitive", [k for k, v in uc.items() if v.get("sensitive")] == ["relay_token"])
    check("marketplace shape: name, owner, entry source '.', no entry version",
          mj["name"] == "hc-voice-relay-claude" and not mj["name"].startswith("claude-") and mj["owner"]["name"]
          and [e["name"] for e in mj["plugins"]] == [pj["name"]] and all(e["source"] == "." and "version" not in e for e in mj["plugins"]))
    claude = shutil.which("claude")
    if not claude:
        check("claude plugin validate (skipped: claude CLI not found)", True)
        return
    check("project instructions live in .claude/CLAUDE.md, not the plugin root", not os.path.exists(os.path.join(ROOT, "CLAUDE.md")))
    p = subprocess.run([claude, "plugin", "validate", "--strict", ROOT], capture_output=True, text=True, timeout=60)
    out = p.stdout + p.stderr
    ok = p.returncode == 0
    if not ok:
        print(out)
    check("claude plugin validate --strict passes", ok)
