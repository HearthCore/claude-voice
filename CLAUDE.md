# claude-voice – Hinweise für Agents

- Python ≥ 3.11, **nur stdlib**. Keine neuen Abhängigkeiten ohne Absprache.
- Tests: `python3 tests/smoke.py` (startet `tests/test_relay.py`, `test_sidecar.py`, `test_hook.py`) muss grün sein, bevor committet wird.
- Committen immer mit expliziten Pfaden, **nie `git add -A` / `git add .`** (Deploy-Dateien wie `.env`, `relay/relay.toml` würden in fremde Commits rutschen).
- Secrets nie ins Repo (`.env` ist ignoriert). Token nie loggen oder ausgeben.
- Der Hook (`hooks/speak.py`) darf Claude Code nie blockieren oder mit Fehlercode beenden.
- Relay-Startregeln (Token Pflicht, `LOCAL_UNSAFE_NO_AUTH` nur Loopback) nicht aufweichen.
- Spezifikation, Entscheidungen, Roadmap: `docs/SPECS.md`. Änderungen am Event-Schema dort nachziehen.
- Produktion: Relay in Docker auf PVE01 (`/home/timo.goetzken/claude-voice`, Container `claude-voice-relay`), öffentlich `https://vox.goetzken.dev/speak`. Update: `git pull --ff-only && docker compose up -d --build`.
- Die Pangolin-Bypass-Regel muss `ACCEPT PATH /speak/*` bleiben (nicht `/speak*`, sonst wird `/speakers…` offen).
- Den Base-Path (`/speak`) behandelt das Relay selbst (`CLAUDE_VOICE_BASE_PATH`); in Pangolin kein Rewrite.
- Nächste Schritte: v0.2 Audio-Fan-out (HA/TV), siehe Roadmap.
