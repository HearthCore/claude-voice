# claude-voice – Hinweise für Agents

- Python ≥ 3.11, **nur stdlib**. Keine neuen Abhängigkeiten ohne Absprache.
- Tests: `python3 tests/smoke.py` (startet `tests/test_relay.py`, `test_sidecar.py`, `test_hook.py`) muss grün sein, bevor committet wird.
- Committen immer mit expliziten Pfaden, **nie `git add -A` / `git add .`** (Deploy-Dateien wie `.env`, `relay/relay.toml` würden in fremde Commits rutschen).
- Secrets nie ins Repo (`.env` ist ignoriert). Token nie loggen oder ausgeben.
- Der Hook (`hooks/speak.py`) darf Claude Code nie blockieren oder mit Fehlercode beenden.
- Relay-Startregeln (Token Pflicht, `LOCAL_UNSAFE_NO_AUTH` nur Loopback) nicht aufweichen.
- Spezifikation, Entscheidungen, Roadmap: `docs/SPECS.md`. Änderungen am Event-Schema dort nachziehen.
- Repo ist öffentlich: keine internen Hostnamen, IPs, Pfade oder Proxy-Interna in versionierte Dateien. Betriebsdetails stehen in `docs/OPERATIONS.local.md` (git-ignoriert, nur lokal).
- Produktion: Relay in Docker hinter Reverse Proxy unter Pfad-Präfix; Bypass-Regel nur `<präfix>/*`; Base-Path behandelt das Relay selbst (`CLAUDE_VOICE_BASE_PATH`), im Proxy kein Rewrite. Update: `git pull --ff-only && docker compose up -d --build`.
- Plugin-Name ist `voice-relay` (`claude-` ist reserviert). Vor jedem Release: `claude plugin validate --strict .` (einzige tolerierte Meldung: `CLAUDE.md` im Root), Version in `plugin.json` und `relay/relay.py` synchron.
- Nächste Schritte: v0.2 Audio-Fan-out (HA/TV), siehe Roadmap.
