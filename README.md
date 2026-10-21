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

## Produktion (Docker hinter Reverse Proxy)

| Komponente | Empfehlung |
|---|---|
| Relay | Docker (`compose.yaml`), Container `claude-voice-relay` |
| Härtung | `restart: unless-stopped`, `read_only`, `cap_drop: ALL`, `init`, `mem_limit 128m`, `pids_limit 128`, Log-Rotation |
| Port | `RELAY_BIND:8787`: nur intern binden (Loopback oder interne Bridge-IP), nie direkt öffentlich |
| Config | `relay/relay.toml` aus `deploy/relay.production.toml`; `.env` (chmod 600) mit Token, optional `CLAUDE_VOICE_BASE_PATH`, `RELAY_BIND` |
| TLS | terminiert der Reverse Proxy (z. B. Pangolin/Traefik) |
| Pfad-Präfix | Relay kann unter einem Präfix (z. B. `/speak`) auf einer geteilten Domain laufen: Proxy-Target mit Prefix-Match, **kein Rewrite**, das Relay entfernt `CLAUDE_VOICE_BASE_PATH` selbst |
| Proxy-Auth | Liegt die Domain hinter Proxy-Auth, eine Bypass-Regel **nur** für `<präfix>/*` anlegen (nicht `<präfix>*`, das öffnet auch `<präfix>ers…`). Dann ist der Relay-Bearer-Token die einzige Auth für diesen Pfad |

Nach dem Deploy prüfen: `<url>/healthz` 200 ohne Token; ohne/mit falschem Token 401; mit Token 200; `<präfix>/../…` und `%2e%2e` dürfen nicht am Proxy-Schutz vorbeikommen.
SSE: `curl -N -H "Authorization: Bearer $TOKEN" "<url>/v1/stream?device=NAME"` muss `: keepalive` im 15-s-Takt zeigen (kein Response-Buffering, Idle-Timeout > 15 s).

**Update:** `git pull --ff-only && docker compose up -d --build`
**Reboot:** Container startet mit Docker selbst (`unless-stopped`).
**Zustand** (Mutes, Queues) liegt nur im Speicher und geht beim Neustart verloren (gewollt, TTL 45 s).

**TTS:** Ein OpenAI-kompatibler Server mit echtem Streaming ist entscheidend für die Latenz (erstes Byte < 1 s). Manche Server liefern bei `stream=true` immer `audio/pcm` (24 kHz s16le mono), egal welches `response_format`; der Sidecar wählt den Player deshalb nach `Content-Type`. Gateways, die erst komplett rendern, „streamen“ nur scheinbar. Der Sidecar wiederholt 5xx vor dem ersten Byte (`TTS_RETRIES`).

### Relay-Env (`.env`)

| Variable | Bedeutung |
|---|---|
| `CLAUDE_VOICE_TOKEN` | Bearer-Token (`openssl rand -hex 32`), nie committen |
| `CLAUDE_VOICE_TOKENS` | Komma-Liste für Rotation (`neu,alt`) |
| `CLAUDE_VOICE_BASE_PATH` | Pfad-Präfix hinter Shared-Proxy (Produktion `/speak`); auch `[server] base_path` in der TOML |
| `RELAY_BIND` | Bind-Adresse des publizierten Ports (Default `127.0.0.1`; hinter Reverse Proxy ggf. interne Bridge-IP) |
| `CLAUDE_VOICE_CONFIG` / `_HOST` / `_PORT` | Alternativen zu `--config` / `--host` / `--port` |
| `LOCAL_UNSAFE_NO_AUTH` | nur lokal auf Loopback, sonst Abbruch |
| `CLAUDE_VOICE_LOG_LEVEL` | `off\|error\|warn\|info\|debug`, Default `info` (siehe „Logging & Datenschutz“) |

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
| `CLAUDE_VOICE_URL` / `_TOKEN` | – | Relay-URL inkl. Präfix, z. B. `https://relay.example.com/speak` |
| `CLAUDE_VOICE_DEVICE` | – (Pflicht) | Gerätename wie in der Relay-Config |
| `TTS_URL` | `http://127.0.0.1:8880/v1` | OpenAI-kompatibles TTS |
| `TTS_MODEL` / `TTS_VOICE` | `kokoro` / `af_heart` | Modell/Stimme des TTS-Servers |
| `TTS_FORMAT` | `pcm` | `pcm\|mp3\|wav\|opus`; Player-Wahl nach `Content-Type` der Antwort |
| `TTS_PCM_RATE` / `TTS_PCM_CHANNELS` | `24000` / `1` | Raw-PCM s16le |
| `TTS_RETRIES` | `2` | Retries bei 5xx/Verbindungsfehler vor dem ersten Byte |
| `TTS_KEY` | – | API-Key des TTS (falls nötig) |
| `TTS_EXTRA_HEADERS` | – | `Name: Wert; Name2: Wert2` (Pangolin `P-Access-Token-Id` / `P-Access-Token`) |
| `TTS_INSTRUCTIONS` | – | Sprechanweisung, wird als `instructions` (OpenAI-Feld) mitgeschickt |
| `TTS_EXTRA_BODY` | – | JSON-Objekt mit providerspezifischen Feldern, z. B. `{"instruct": "ruhig", "seed": 42}`; `input`/`stream` nicht überschreibbar, max. 8 KiB, Werte werden nie geloggt |
| `CLAUDE_VOICE_PLAYER_CMD` | – | Player-Override, sonst `mpv` → `ffplay` |
| `CLAUDE_VOICE_PLAYER_DEADLINE_S` | `180` | Watchdog: hängender Player wird beendet |
| `CLAUDE_VOICE_LOG_LEVEL` | `info` | `off\|error\|warn\|info\|debug` (siehe „Logging & Datenschutz“) |

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
| `CLAUDE_VOICE_NESTED` | `skip` | `skip`: Hooks verschachtelter Hilfs-Sessions (ein `claude` unterhalb eines anderen `claude`, z. B. `claude -p` aus dem Hook eines anderen Plugins; Erkennung über /proc-Abstammung, nur Linux) sprechen nie; `speak`: immer |
| `CLAUDE_VOICE_NOTIFY_TYPES` | `permission_prompt,elicitation_dialog,agent_needs_input` | Notification-Typen, die gesprochen werden; `all` = alle (auch ohne Typ), `none` = keine |
| `CLAUDE_VOICE_AUTO_UPDATE` | an | `0` schaltet das automatische Update beim Session-Start ab (das explizite `voice update` bleibt möglich) |
| `CLAUDE_VOICE_STOP_MODE` | `final` | `final`: Stop-Zwischenmeldungen bei laufenden Hintergrund-Subagents (`background_tasks`) nicht sprechen; `all`: jedes Stop sprechen |
| `CLAUDE_VOICE_LOG_LEVEL` | `off` | `off\|error\|warn\|info\|debug`; JSON-Zeilen in `$CLAUDE_VOICE_STATE_DIR/hook.log` (0600, Rotation bei 256 KiB → `hook.log.1`) |

Slash-Command: `/voice status | mute | unmute | speak <text>`.

**Mute:** Client sofort/offline: `voicectl local mute [--session SID]`; Relay: `voicectl mute --scope all|session|device|group --id X`.

## Cloud (claude.ai Environment)

| Schritt | Wert |
|---|---|
| Setup-Script | `curl -fsSL https://raw.githubusercontent.com/HearthCore/claude-voice/<Tag>/deploy/cloud-env-setup.sh \| bash -s -- <Tag> \|\| true` (Tag als Argument: Environment-Variablen sind im Setup-Script nicht zuverlässig sichtbar; Checkout immer detached + Markierung `.git/claude-voice-managed`) oder Inhalt von `deploy/cloud-env-setup.sh` (läuft als root vor Claude Code; endet immer mit Exit 0; idempotenter Clone/Fetch nach `~/.claude-plugins/claude-voice`; Environment-Cache ~7 Tage) |
| Updates | `CLAUDE_VOICE_REF` ändern → greift beim nächsten Session-Start automatisch (`hooks/self_update.py`, nur bei detached oder vom Setup-Script markiertem Checkout ohne lokale Änderungen; Entwicklungs-Checkouts auf einem Branch bleiben unangetastet); sofort: `/claude-voice:voice update <tag>`; `/claude-voice:voice version` zeigt den Stand. Hook-Skripte werden bei jedem Aufruf frisch von der Platte geladen, nur Änderungen an `hooks.json` brauchen eine neue Session. Abschalten: `CLAUDE_VOICE_AUTO_UPDATE=0`. Immer einen **Tag** als Ref verwenden: ein bereits lokal bekannter Branch (z. B. `main`) wird nicht erneut geholt |
| Aktivierung | Env `CLAUDE_CODE_PLUGIN_DIRS=~/.claude-plugins/claude-voice` (Env-Pendant zu `--plugin-dir`, Claude Code ≥ 2.1.280, Plugin lädt als `claude-voice@inline`); `extraKnownMarketplaces` aus `.claude/settings.json` funktioniert in Cloud-Sessions **nicht** |
| Env | `CLAUDE_VOICE_URL=https://relay.example.com/speak`, `CLAUDE_VOICE_TOKEN` (oder Network Secret, s. u.), `CLAUDE_VOICE_REF=<Release-Tag>` (Tag pinnen, empfohlen); optional `CLAUDE_VOICE_REPO`, `CLAUDE_VOICE_PLUGIN_DIR` |
| Privates Repo | `CLAUDE_VOICE_GH_TOKEN` = Fine-grained PAT, nur dieses Repo, Contents read-only (läuft per `GIT_CONFIG_*`-Header, nie in URL/argv/Disk). Alternative: Repo öffentlich machen (enthält keine Secrets), PAT entfällt |
| Netzwerk | Level „Trusted" erreicht eigene Domains nicht. Entweder Custom-Allowlist mit der Relay-Domain (Token dann als Env `CLAUDE_VOICE_TOKEN`) oder (sicherer) **Network Secret**: Host = Relay-Domain, Header `Authorization`, Prefix `Bearer`, Wert = Relay-Token. Der Agent-Proxy hängt ihn an, der Host ist bei jedem Level erreichbar, der Token gelangt nie in die VM. Dann `CLAUDE_VOICE_TOKEN` **nicht** setzen (Hook sendet keinen Header, Proxy ergänzt ihn) |
| Hinweis | Network Secrets werden nicht an Requests des Setup-Scripts angehängt (hier irrelevant) |

Hook in der Cloud: fork + `setsid`, das Kind leitet fd 0–2 auf `/dev/null` (sonst wartet Claude Code auf die Pipes: gemessen 5,26 s vorher, 0,05 s nachher); kein TTY nötig; `urllib` respektiert `HTTPS_PROXY`.

## Logging & Datenschutz

Eine Variable für alle Komponenten: `CLAUDE_VOICE_LOG_LEVEL` (Relay/Sidecar Default `info`, Hook Default `off`). Relay und Sidecar loggen nach stdout (journald/Docker), der Hook in `hook.log`.

| Level | Inhalt |
|---|---|
| `off` | nichts (nur FATAL-Startfehler von Relay/Sidecar) |
| `error` | Fehler |
| `warn` | + Degradierung: TTS-Retry, Relay nicht erreichbar, 4xx/5xx, verworfene/abgelaufene Events, verdrängte Streams |
| `info` | + Betrieb: Verbindungen, Event-ID, Gerät, Status, Zeichen**anzahl**, Requests. **Kein Nachrichtentext** |
| `debug` | + vollständiger Inhalt: Hook-Input/Rohtext/bereinigter Text/Payload/POST-Ergebnis, Relay-Body und Zustellung, Sidecar-Text je Satz, TTS-Request (ohne Key/Header-Werte), Content-Type, TTFB, Player-Argv |

Bei `debug` loggen Relay und Sidecar eine Warnung beim Start. Tokens, `Authorization`, `TTS_KEY`, `TTS_EXTRA_HEADERS`- und `TTS_EXTRA_BODY`-Werte werden auf keinem Level geloggt (durch Test abgesichert).

## Sicherheit (Kurzfassung)

Tokens, Keys und Extra-Header gehen nie über Klartext-http an Nicht-Loopback-Hosts; Redirects werden nie verfolgt (Hook, voicectl, Sidecar). Details und Grenzen: [docs/SPECS.md](docs/SPECS.md).

## Tests

```bash
python3 tests/smoke.py      # 203 Checks: tests/test_relay.py, test_sidecar.py, test_hook.py, test_logging.py
```

Fake-TTS, tee-/Stub-Player, freie Ports; kein Netzwerk nötig. Repo-Agents: `.claude/agents/reviewer.md` (read-only), `scout.md` (haiku, read-only).

Spezifikation, Entscheidungen, Roadmap: [docs/SPECS.md](docs/SPECS.md).
