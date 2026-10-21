# claude-voice

Claude-Code-Antworten auf deinen Geräten vorlesen: **Hook → Relay → (SSE) → Sidecar → lokales TTS → Player**.
Ein Repo, eine Version (v0.1.0): Plugin (Hook + Sidecar + CLI) und Relay-Server. Nur Python ≥ 3.11 stdlib.

```
Claude Code (lokal oder Cloud)
  └─ Stop/Notification-Hook ── POST /v1/speak (fire-and-forget, async)
                                   ▼
                              relay  (Bearer-Auth, Gruppen, Mute, TTL)   — verarbeitet in v0.1 kein Audio
                                   ▲  SSE, dauerhaft AUSGEHEND vom Gerät (kein Polling, Keepalive 15 s,
                                   │  Reconnect mit Backoff 1 → 30 s, kein offener Port am Notebook)
                          sidecar (omarchy-notebook)
                          → eigenes TTS (Stimme/Modell pro Gerät) → mpv
```

## Quickstart lokal (ohne Auth, nur Loopback)

```bash
cp relay/relay.example.toml relay/relay.toml
LOCAL_UNSAFE_NO_AUTH=true python3 relay/relay.py --config relay/relay.toml    # 127.0.0.1:8787, bei belegtem Port: --port 8788

export CLAUDE_VOICE_URL=http://127.0.0.1:8787 CLAUDE_VOICE_DEVICE=omarchy-notebook
export TTS_URL=http://127.0.0.1:8880/v1 TTS_VOICE=af_heart     # dein OpenAI-kompatibles TTS
python3 sidecar/sidecar.py &
python3 tools/voicectl.py speak "Hallo Timo" --target omarchy-notebook
```

`LOCAL_UNSAFE_NO_AUTH=true` wird **verweigert**, sobald der Host nicht Loopback ist. Ohne Token startet das Relay nicht (Token ≥ 16 Zeichen).

## Produktion (Ist-Zustand)

| Komponente | Wert |
|---|---|
| Relay | Docker auf PVE01, Repo `/home/timo.goetzken/claude-voice`, Container `claude-voice-relay` |
| Härtung | `restart: unless-stopped`, `read_only`, `cap_drop: ALL`, `init`, `mem_limit 128m`, `pids_limit 128`, Log-Rotation |
| Port | `10.0.2.1:8787` (`RELAY_BIND`, Pangolin-Konvention auf PVE01) |
| Config | `relay/relay.toml` aus `deploy/relay.pve01.toml` (nur `omarchy-notebook` aktiv); `.env` (chmod 600) mit Token, `CLAUDE_VOICE_BASE_PATH=/speak`, `RELAY_BIND` |
| Öffentliche URL | `https://vox.goetzken.dev/speak` (nutzt die bestehende vox-Pangolin-Resource) |
| Pangolin | Target `10.0.2.1:8787`, Site PVE01, Pfad-Präfix `/speak`, **kein Rewrite** (Relay entfernt den Base-Path selbst) |
| Access-Regel | in der vox-Resource-Policy: `ACCEPT PATH /speak/*` (umgeht Pangolin-Header-Auth; für `/speak/*` ist der Relay-Bearer-Token die einzige Auth) |
| TTS | `https://vox.goetzken.dev/v1`, Modell `sopro`, Stimme `timo_de` (weitere: jarvis, friday, sophie, beth, timo_en …), Pangolin-Header-Auth via `TTS_EXTRA_HEADERS` |

Regel muss `/speak/*` bleiben, **nicht** `/speak*` (würde auch `/speakers…` öffnen).
Geprüft: `/speak/healthz` 200 ohne Token; ohne/mit falschem Token 401; mit Token 200; `/speak/../v1/models`, `%2e%2e`, `/speakers` → 401 (vox bleibt geschützt).
SSE durch Traefik/Pangolin: Keepalives exakt alle 15 s, kein Buffering (Traefik `readTimeout` 30 m / `idleTimeout` 5 m unkritisch dank Keepalive).

**Update:** `git pull --ff-only && docker compose up -d --build`
**Reboot:** docker + containerd sind enabled, der Container startet selbst (noch nicht per echtem Reboot getestet).
**Zustand** (Mutes, Queues) liegt nur im Speicher und geht beim Neustart verloren (gewollt, TTL 45 s).

**TTS-Wahl:** vox streamt echt (erstes Byte 0,25–0,9 s) und liefert mit `stream=true` immer `audio/pcm` 24 kHz s16le mono. LiteLLM (`llm.goetzken.dev/v1`) bedient `sopro` zwar auch, streamt aber nur scheinbar (erstes Byte ~3,6 s) und liefert ~50 % 502 → nicht verwendet. Der Sidecar wiederholt 5xx vor dem ersten Byte (`TTS_RETRIES`).

### Relay-Env (`.env`)

| Variable | Bedeutung |
|---|---|
| `CLAUDE_VOICE_TOKEN` | Bearer-Token (`openssl rand -hex 32`), nie committen |
| `CLAUDE_VOICE_TOKENS` | Komma-Liste für Rotation (`neu,alt`) |
| `CLAUDE_VOICE_BASE_PATH` | Pfad-Präfix hinter Shared-Proxy (Produktion `/speak`); auch `[server] base_path` in der TOML |
| `RELAY_BIND` | Bind-Adresse des publizierten Ports (Default `127.0.0.1`, PVE01 `10.0.2.1`) |
| `CLAUDE_VOICE_CONFIG` / `_HOST` / `_PORT` | Alternativen zu `--config` / `--host` / `--port` |
| `LOCAL_UNSAFE_NO_AUTH` | nur lokal auf Loopback, sonst Abbruch |

## Notebook (Omarchy): Sidecar als systemd-User-Service

```bash
mkdir -p ~/.config/claude-voice && cp deploy/sidecar.env.example ~/.config/claude-voice/sidecar.env
chmod 600 ~/.config/claude-voice/sidecar.env      # Token + TTS-Header eintragen, CLAUDE_VOICE_HOME = Repo-Pfad
mkdir -p ~/.config/systemd/user && cp deploy/claude-voice-sidecar.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now claude-voice-sidecar
journalctl --user -u claude-voice-sidecar -f      # Logs
```

`Restart=always`. `loginctl enable-linger` ist nicht nötig (Audio braucht ohnehin die User-Session).

### Sidecar-Env (`~/.config/claude-voice/sidecar.env`)

| Variable | Default | Bedeutung |
|---|---|---|
| `CLAUDE_VOICE_HOME` | – | Repo-Pfad (nur für die systemd-Unit) |
| `CLAUDE_VOICE_URL` / `_TOKEN` | – | Relay (Produktion `https://vox.goetzken.dev/speak`) |
| `CLAUDE_VOICE_DEVICE` | – (Pflicht) | Gerätename wie in der Relay-Config |
| `TTS_URL` | `http://127.0.0.1:8880/v1` | OpenAI-kompatibles TTS |
| `TTS_MODEL` / `TTS_VOICE` | `kokoro` / `af_heart` | Produktion `sopro` / `timo_de` |
| `TTS_FORMAT` | `pcm` | `pcm\|mp3\|wav\|opus`; Player-Wahl nach `Content-Type` der Antwort |
| `TTS_PCM_RATE` / `TTS_PCM_CHANNELS` | `24000` / `1` | Raw-PCM s16le |
| `TTS_RETRIES` | `2` | Retries bei 5xx/Verbindungsfehler vor dem ersten Byte |
| `TTS_KEY` | – | API-Key des TTS (falls nötig) |
| `TTS_EXTRA_HEADERS` | – | `Name: Wert; Name2: Wert2` (Pangolin `P-Access-Token-Id` / `P-Access-Token`) |
| `CLAUDE_VOICE_PLAYER_CMD` | – | Player-Override, sonst `mpv` → `ffplay` |
| `CLAUDE_VOICE_PLAYER_DEADLINE_S` | `180` | Watchdog: hängender Player wird beendet |

## Hook / Plugin

```bash
claude --plugin-dir /pfad/zu/claude-voice        # lokal testen
```

Stop: spricht bevorzugt `last_assistant_message` (Transcript nur Fallback); Notification: `message`. Code, Tabellen, URLs werden entfernt; reine Code-/Tabellen-Antworten senden nichts; Kürzung bei `CLAUDE_VOICE_MAX_CHARS` an einer Satzgrenze. `hooks.json`: `async: true`, `timeout: 10`.

| Variable | Default | Bedeutung |
|---|---|---|
| `CLAUDE_VOICE_URL` | – | Relay-URL; ohne sie ist der Hook ein No-op |
| `CLAUDE_VOICE_TOKEN` | – | Bearer-Token (in der Cloud besser per Network Secret, s. u.) |
| `CLAUDE_VOICE_TARGET` | Relay-Routing | Gerät/Gruppe |
| `CLAUDE_VOICE_MAX_CHARS` | `500` | Max. gesprochene Zeichen |
| `CLAUDE_VOICE_TIMEOUT` / `_RETRIES` | `2` / `1` | HTTP-Timeout (s) / Wiederholungen |
| `CLAUDE_VOICE_STATE_DIR` | `~/.claude-voice` | lokale Mute-Flags |
| `CLAUDE_VOICE_SYNC` | – | Debug: ohne fork/setsid, synchron |

Slash-Command: `/voice status | mute | unmute | speak <text>`.

**Mute:** Client sofort/offline: `voicectl local mute [--session SID]`; Relay: `voicectl mute --scope all|session|device|group --id X`.

## Cloud (claude.ai Environment „GDev")

| Schritt | Wert |
|---|---|
| Setup-Script | Inhalt von `deploy/cloud-env-setup.sh` (läuft als root vor Claude Code; endet immer mit Exit 0; idempotenter Clone/Fetch nach `~/.claude-plugins/claude-voice`; Environment-Cache ~7 Tage, Updates kommen beim Cache-Rebuild) |
| Aktivierung | Env `CLAUDE_CODE_PLUGIN_DIRS=~/.claude-plugins/claude-voice` (Env-Pendant zu `--plugin-dir`, Claude Code ≥ 2.1.280, Plugin lädt als `claude-voice@inline`); `extraKnownMarketplaces` aus `.claude/settings.json` funktioniert in Cloud-Sessions **nicht** |
| Env | `CLAUDE_VOICE_URL=https://vox.goetzken.dev/speak`, `CLAUDE_VOICE_REF=v0.1.0` (Tag pinnen, empfohlen); optional `CLAUDE_VOICE_REPO`, `CLAUDE_VOICE_PLUGIN_DIR` |
| Privates Repo | `CLAUDE_VOICE_GH_TOKEN` = Fine-grained PAT, nur dieses Repo, Contents read-only (läuft per `GIT_CONFIG_*`-Header, nie in URL/argv/Disk). Alternative: Repo öffentlich machen (enthält keine Secrets), PAT entfällt |
| Netzwerk | Level „Trusted" erreicht `vox.goetzken.dev` nicht. Entweder Custom-Allowlist mit `vox.goetzken.dev` oder (bevorzugt) **Network Secret**: Host `vox.goetzken.dev`, Header `Authorization`, Prefix `Bearer`, Wert = Relay-Token. Der Agent-Proxy hängt ihn an, der Host ist bei jedem Level erreichbar, der Token gelangt nie in die VM. Dann `CLAUDE_VOICE_TOKEN` **nicht** setzen (Hook sendet keinen Header, Proxy ergänzt ihn) |
| Hinweis | Network Secrets werden nicht an Requests des Setup-Scripts angehängt (hier irrelevant) |

Hook in der Cloud: fork + `setsid`, das Kind leitet fd 0–2 auf `/dev/null` (sonst wartet Claude Code auf die Pipes: gemessen 5,26 s vorher, 0,05 s nachher); kein TTY nötig; `urllib` respektiert `HTTPS_PROXY`.

## Sicherheit (Kurzfassung)

Tokens, Keys und Extra-Header gehen nie über Klartext-http an Nicht-Loopback-Hosts; Redirects werden nie verfolgt (Hook, voicectl, Sidecar). Details und Grenzen: [docs/SPECS.md](docs/SPECS.md).

## Tests

```bash
python3 tests/smoke.py      # 123 Checks: tests/test_relay.py, test_sidecar.py, test_hook.py
```

Fake-TTS, tee-/Stub-Player, freie Ports; kein Netzwerk nötig. Repo-Agents: `.claude/agents/reviewer.md` (read-only), `scout.md` (haiku, read-only).

Spezifikation, Entscheidungen, Roadmap: [docs/SPECS.md](docs/SPECS.md).
