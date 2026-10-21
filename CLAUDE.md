# claude-voice – Hinweise für Agents

- Python ≥ 3.11, **nur stdlib**. Keine neuen Abhängigkeiten ohne Absprache.
- Tests: `python3 tests/smoke.py` muss grün sein, bevor committet wird.
- Secrets nie ins Repo (`.env` ist ignoriert). Token nie loggen oder ausgeben.
- Der Hook (`hooks/speak.py`) darf Claude Code nie blockieren oder mit Fehlercode beenden.
- Relay-Startregeln (Token Pflicht, `LOCAL_UNSAFE_NO_AUTH` nur Loopback) nicht aufweichen.
- Spezifikation und Roadmap: `docs/SPECS.md`. Änderungen am Event-Schema dort nachziehen.
- Nächste Schritte: v0.2 Audio-Fan-out (HA/TV), siehe Roadmap.
