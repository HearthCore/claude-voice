# claude-voice

Claude-Code-Antworten auf deinen Geräten vorlesen: **Hook → Relay → Sidecar → OpenAI-kompatibles TTS (Streaming)**.
Ein Repo, eine Version: Plugin (Hook + Sidecar + CLI) und Relay-Server.

```
Claude Code (lokal oder Cloud)
  └─ Stop/Notification-Hook ── 1× POST /v1/speak (fire-and-forget, 2 s Timeout, 1 Retry)
                                   ▼
                              relay  (Bearer-Auth, Gruppen, Mute, TTL)
                                   ▲  SSE, ausgehend vom Gerät (kein Port am Notebook offen)
              ┌────────────────────┼─────────────────┐
         sidecar (Omarchy)    sidecar (weiteres)   [v0.2] HA / TV Subscriber
         → /v1/audio/speech (stream) → mpv/ffplay
```

Nur Python ≥ 3.11 Standardbibliothek. Keine Abhängigkeiten.

## Quickstart lokal (ohne Auth, nur Loopback)

```bash
cp relay/relay.example.toml relay/relay.toml
LOCAL_UNSAFE_NO_AUTH=true python3 relay/relay.py --config relay/relay.toml        # 127.0.0.1:8787

export CLAUDE_VOICE_URL=http://127.0.0.1:8787 CLAUDE_VOICE_DEVICE=omarchy-notebook
export TTS_URL=http://127.0.0.1:8880/v1 TTS_VOICE=af_heart     # dein OpenAI-kompatibler TTS
python3 sidecar/sidecar.py &

python3 tools/voicectl.py speak "Hallo Timo" --target omarchy-notebook
```

`LOCAL_UNSAFE_NO_AUTH=true` wird **verweigert**, sobald der Host nicht Loopback ist.

## Remote (Token Pflicht)

```bash
openssl rand -hex 32            # -> CLAUDE_VOICE_TOKEN
cp .env.example .env            # Token eintragen, NIE committen
docker compose up -d --build    # Relay hinter Pangolin/TLS
```

Rotation: `CLAUDE_VOICE_TOKENS=neu,alt` (kommagetrennt), Clients umstellen, `alt` entfernen.

## Plugin in Claude Code

```bash
claude --plugin-dir /pfad/zu/claude-voice        # lokal testen
```

Env für die Hooks (Cloud-Environment: als Environment-Variablen setzen, nicht ins Repo):

| Variable | Bedeutung |
|---|---|
| `CLAUDE_VOICE_URL` | Relay-URL, **ohne** diese Variable ist der Hook ein No-op |
| `CLAUDE_VOICE_TOKEN` | Bearer Token |
| `CLAUDE_VOICE_TARGET` | Gerät/Gruppe, sonst Routing des Relays (`routes.claude-code`) |
| `CLAUDE_VOICE_MAX_CHARS` | Max. gesprochene Zeichen (Default 500) |

Slash-Command: `/voice status | mute | unmute | speak <text>`.

## Mute

| Ebene | Wie |
|---|---|
| Client (sofort, offline-sicher) | `voicectl local mute [--session SID]` |
| Relay: alles / Session / Gerät / Gruppe | `voicectl mute --scope all\|session\|device\|group --id X` |

## Sidecar-Env

`CLAUDE_VOICE_DEVICE` (Pflicht), `TTS_URL`, `TTS_KEY`, `TTS_MODEL`, `TTS_VOICE`, `TTS_FORMAT`, `CLAUDE_VOICE_PLAYER_CMD`.
Player-Auswahl: `mpv` → `ffplay`, sonst Fehler.

## Tests

```bash
python3 tests/smoke.py      # 33 Checks: Auth, Gruppen, Mute, TTL, Sidecar-Streaming, Hook
```

Details und Roadmap: [docs/SPECS.md](docs/SPECS.md).
