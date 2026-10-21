# SPECS claude-voice

## Ziele
1. Claude-Code-Antworten (lokal und Cloud-Environment) auf einem oder mehreren Geräten hörbar machen.
2. Hook blockiert die Session nie und scheitert nie laut.
3. Geräte verbinden sich **ausgehend** (SSE). Kein offener Port, kein Tunnel zum Notebook.
4. Gruppen (`home = [homeassistant, smarttv, omarchy-notebook]`), Routing, Mute auf mehreren Ebenen.
5. Plugin und Relay im selben Repo, gleiche Version.

## Nicht-Ziele (v0.1)
- Synchrone Wiedergabe über Geräte (nur Vorbereitung: `play_at`, `latency_offset_ms`).
- Home-Assistant-/TV-Subscriber (brauchen Audio-URL-Fan-out, siehe Roadmap).
- Persistenz über Relay-Neustart (Events, Mutes, Queues leben im Speicher).
- Audio im Relay: in v0.1 synthetisiert jeder Sidecar lokal (Stimme/Modell pro Gerät).

## Event-Schema (`POST /v1/speak`)
```json
{ "text": "Fertig, Tests grün.", "target": "home", "source": "claude-code",
  "session_id": "cc-abc", "priority": "normal", "ttl_s": 45, "play_at": null }
```
`target`: Gerät | Gruppe | `active` | `all`. Fehlt es, gilt `routes[source]`, sonst `defaults.route`. Felder ≤ 128 Zeichen, `text` ≤ 4000.
`active` = Gerät mit jüngster Verbindung bzw. Auslieferung.
Antwort `202`: ein Event pro Zielgerät (`id`, `device`, `status`: `queued|muted:<scope>|dropped:queue_full`).

**SSE-Event `speak`** (an das Gerät):

| Feld | Bedeutung |
|---|---|
| `id`, `text`, `source`, `session_id`, `priority` | wie gesendet |
| `volume`, `latency_offset_ms` | aus der Gerätekonfig |
| `play_at` | optional; Sidecar wartet max. 5 s (`MAX_PLAY_AT_WAIT_S`) |
| `expires_in_s` | Restlaufzeit relativ zum Sendezeitpunkt; Clients verlassen sich nicht auf die Wanduhr |

**Ack (`POST /v1/ack`):** `{id, device, status}`, `status` ∈ `delivered | played | tts_error | player_error | expired | acked`.
Lebenszyklus je Gerät: `queued → delivered → played | tts_error | player_error | expired`. `delivered` setzt das Relay, sobald das Event in den Socket geschrieben ist.

## API
Alle Pfade zusätzlich unter `CLAUDE_VOICE_BASE_PATH` (Produktion `/speak`, z. B. `/speak/v1/speak`); das Relay entfernt das Präfix selbst, unpräfixierte Pfade funktionieren weiter.

| Methode | Pfad | Auth | Zweck |
|---|---|---|---|
| GET | `/healthz` | nein | Liveness |
| GET | `/v1/stream?device=NAME` | ja | SSE, Keepalive alle 15 s; neueste Verbindung je Gerät gewinnt; max. 64 gleichzeitige Streams (sonst 503) |
| POST | `/v1/speak` | ja | Event erzeugen |
| POST | `/v1/ack` | ja | Gerät meldet Status |
| POST | `/v1/mute` | ja | `{scope: all\|session\|device\|group, id, muted}` |
| GET | `/v1/state` | ja | Geräte, Verbindung, Mutes, letzte Events |

## Sicherheit
| Bereich | Verhalten |
|---|---|
| Auth | Bearer; Vergleich über sha256 + `hmac.compare_digest`; Token ≥ 16 Zeichen; mehrere Tokens zur Rotation |
| Start | Ohne Token kein Start; `LOCAL_UNSAFE_NO_AUTH=true` nur Loopback, sonst Abbruch (Exit 2) |
| Limits | Body 64 KiB, Text 4000, Felder ≤ 128, Ack-Status per Whitelist, `by_id` (1000) / History / Mutes begrenzt, max. 64 Streams, Socket-Timeout 30 s |
| Fehler/Logs | generischer 500 ohne Interna; Steuerzeichen in Logs escaped; keine Tokens in Logs oder URLs |
| Clients (Hook, voicectl, Sidecar) | Token/Keys/Extra-Header nie über Klartext-http an Nicht-Loopback-Hosts; Redirects nie verfolgt |
| Hook | bereinigt Code, URLs, Tabellen; nur Assistant-Text, keine Dateiinhalte oder Tool-Outputs; blockiert nie |
| TLS | terminiert Pangolin/Traefik |
| Pangolin-Bypass | nur `PATH /speak/*` (nicht `/speak*`); Relay-Token ist dort die einzige Auth |

## Bekannte Grenzen v0.1
- Ein gemeinsamer Token: jeder Token-Inhaber kann sich als beliebiges Gerät subscriben (Per-Device-Tokens: Roadmap).
- `delivered` gilt beim Schreiben in den Socket; bei halb offener Verbindung (Laptop-Standby) kann ein Event bis zum 30-s-Write-Timeout verloren gehen (begrenzt durch TTL; Ack-basierte Redelivery: Roadmap).
- Kein Rate-Limit auf 401 (Token 64 Hex-Zeichen).
- `play_at` wartet max. 5 s; echte Synchronität erst v0.3.

## Entscheidungen
| Thema | Entscheidung | Grund |
|---|---|---|
| Transport | Pull per SSE | Notebook oft zu, NAT-freundlich, kein Fan-out im Hook |
| Fan-out | Relay erzeugt pro Gerät ein eigenes Event | Eigenes Ack/TTL, Teilausfall blockiert nichts |
| Veraltete Events | TTL, danach verworfen | Nichts 10 Minuten später vorlesen |
| Routing-Default | `active` | Kein Echo auf allen Geräten |
| Stack | Python stdlib | Null Abhängigkeiten, läuft überall |
| TTS | Server mit echtem Streaming (nicht über ein rendernd puffernes Gateway) | erstes Byte 0,25–0,9 s statt ~3,6 s bei Fake-Streaming |
| Audioformat | PCM, Player nach `Content-Type` | `stream=true` liefert immer `audio/pcm` 24 kHz s16le mono, egal welches `response_format`; Raw-PCM-Flags für mpv/ffplay |
| Öffentlicher Pfad | Präfix (z. B. `/speak`) auf einer bestehenden, geschützten Proxy-Resource mit Bypass nur für `<präfix>/*` | keine neue Domain/Resource; Bypass so eng wie möglich |
| Base-Path | im Relay (`CLAUDE_VOICE_BASE_PATH`) statt Proxy-Rewrite | kein Rewrite-Setup in Pangolin, Pfad-Traversal wird im Relay strikt behandelt |
| Cloud-Aktivierung | `CLAUDE_CODE_PLUGIN_DIRS` | Marketplace-Einträge aus `.claude/settings.json` greifen in Cloud-Sessions nicht |
| Cloud-Token | bevorzugt Network Secret für die Relay-Domain (`Authorization: Bearer`), alternativ Env `CLAUDE_VOICE_TOKEN` | Network Secret: Token gelangt nie in die VM |
| Cloud-Hook | fork + `setsid`, fd 0–2 auf `/dev/null`, `async: true` | sonst wartet Claude Code auf Pipes (5,26 s statt 0,05 s) |

## Roadmap
- **v0.2 Audio-Fan-out:** TTS-Worker im Relay rendert satzweise einmal, verteilt kurzlebige Audio-URLs → HA (`media_player.play_media`/`tts.speak`) und Smart TV. Gleiche Stimme, 1× TTS-Last.
- **v0.3 Synchronität:** `play_at` (NTP) + `latency_offset_ms` pro Gerät auswerten.
- **Presence:** `home` nur bei Anwesenheit (HA), sonst Fallback `active`.
- **Barge-in:** neues Event bricht laufende Wiedergabe derselben Session ab.
- **Per-Device-Tokens:** ein Token je Gerät statt gemeinsamem Token.
- **Ack-basierte Redelivery:** `delivered` erst nach Ack, sonst erneut zustellen (innerhalb TTL).
- ~~Claude-Code-Cloud-Environment~~ — **erledigt in v0.1** (`deploy/cloud-env-setup.sh`, siehe README).
