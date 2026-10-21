#!/usr/bin/env bash
# claude-voice: setup script for a claude.ai cloud environment (e.g. "GDev").
#
# Paste into the environment's "Setup script" field (or `curl … | bash` it from there).
# Runs as root before Claude Code starts. It is idempotent and NEVER fails the session:
# every error is logged and the script exits 0.
#
# Activation: set CLAUDE_CODE_PLUGIN_DIRS=~/.claude-plugins/claude-voice in the
# environment variables (the env equivalent of --plugin-dir, loads as claude-voice@inline).
#
# Later updates need no change of this (cached) script: hooks/self_update.py moves the detached checkout to
# CLAUDE_VOICE_REF at session start, or on demand via /claude-voice:voice update <tag>.
#
# Env (all optional, no secrets in this file):
#   CLAUDE_VOICE_REPO        default https://github.com/HearthCore/claude-voice.git
#   CLAUDE_VOICE_REF         branch or tag, default main. Recommended: a release tag (e.g. v0.1.0),
#                            so a moving branch never runs unreviewed code in every session.
#   CLAUDE_VOICE_PLUGIN_DIR  default $HOME/.claude-plugins/claude-voice
#   CLAUDE_VOICE_GH_TOKEN    fine-grained PAT, read-only "Contents" on this one repo
#                            (only needed while the repo is private; never written to disk)
[ -n "${BASH_VERSION:-}" ] || { echo "[claude-voice-setup] needs bash, skipping"; exit 0; }
# No `set -e/-u`: an unbound variable would abort the shell and with it the session start.

REPO="${CLAUDE_VOICE_REPO:-https://github.com/HearthCore/claude-voice.git}"
REF="${CLAUDE_VOICE_REF:-main}"
DIR="${CLAUDE_VOICE_PLUGIN_DIR:-$HOME/.claude-plugins/claude-voice}"

log() { echo "[claude-voice-setup] $*"; }
# `timeout` is coreutils; never let its absence silently skip the clone.
to() { if command -v timeout >/dev/null; then timeout "$@"; else shift; "$@"; fi; }

git_auth() {
  # Token goes in via an ephemeral http header, never into the URL, .git/config or the process list.
  if [ -n "${CLAUDE_VOICE_GH_TOKEN:-}" ]; then
    local b64
    b64=$(printf 'x-access-token:%s' "$CLAUDE_VOICE_GH_TOKEN" | base64 -w0)
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
      GIT_CONFIG_VALUE_0="Authorization: Basic $b64" GIT_TERMINAL_PROMPT=0 git "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

main() {
  command -v git >/dev/null || { log "git missing, skipping"; return 0; }
  command -v python3 >/dev/null || log "warning: python3 missing, the hook will be a no-op"
  mkdir -p "$(dirname "$DIR")" || return 0

  if [ -d "$DIR/.git" ]; then
    log "updating $DIR ($REF)"
    to 60 bash -c "$(declare -f git_auth); $(declare -p REF DIR); \
      git_auth -C \"\$DIR\" fetch --depth 1 origin \"\$REF\" && git -C \"\$DIR\" checkout -q --detach FETCH_HEAD" \
      || log "update failed, keeping the existing checkout"
  elif [ -e "$DIR" ]; then
    log "$DIR exists but is not a git checkout, leaving it alone"; return 0
  else
    log "cloning $REPO ($REF) -> $DIR"
    local tmp
    tmp=$(mktemp -d "$(dirname "$DIR")/.claude-voice.XXXXXX") || return 0
    if to 90 bash -c "$(declare -f git_auth); $(declare -p REF REPO tmp); \
         git_auth clone -q --depth 1 --branch \"\$REF\" \"\$REPO\" \"\$tmp/repo\""; then
      mv "$tmp/repo" "$DIR"
    else
      log "clone failed (private repo without CLAUDE_VOICE_GH_TOKEN? network?)"
    fi
    rm -rf "$tmp"
    [ -d "$DIR" ] || return 0
  fi

  # Hook runs as the session user; make sure it can read the checkout.
  chmod -R a+rX "$DIR" 2>/dev/null || true
  [ -f "$DIR/hooks/speak.py" ] && log "ok: $(git -C "$DIR" log -1 --format='%h %s' 2>/dev/null)"
  [ -n "${CLAUDE_VOICE_URL:-}" ] || log "note: CLAUDE_VOICE_URL not set, the hook stays silent"
  local dirs="${CLAUDE_CODE_PLUGIN_DIRS:-}"
  case ":${dirs//\~/$HOME}:" in
    *":$DIR:"*) ;;
    *) log "note: add CLAUDE_CODE_PLUGIN_DIRS=$DIR to the environment variables to activate the plugin" ;;
  esac
}

main || true
exit 0
