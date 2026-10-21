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
- Persistenz über Relay-Neustart (Events leben ≤ TTL im Speicher).

## Event-Schema (`POST /v1/speak`)
```json
{ "text": "Fertig, Tests grün.", "target": "home", "source": "claude-code",
  "session_id": "cc-abc", "priority": "normal", "ttl_s": 45, "play_at": null }
```
`target`: Gerät | Gruppe | `active` | `all`. Fehlt es, gilt `routes[source]`, sonst `defaults.route`.
Antwort `202`: ein Event pro Zielgerät (`id`, `device`, `status`: `queued|muted:<scope>|dropped:queue_full`).
SSE-Event `speak` an das Gerät ergänzt `volume`, `latency_offset_ms` aus der Gerätekonfig.
Status-Lebenszyklus je Gerät: `queued → delivered → played | tts_error | expired`.

## API
| Methode | Pfad | Auth | Zweck |
|---|---|---|---|
| GET | `/healthz` | nein | Liveness |
| GET | `/v1/stream?device=NAME` | ja | SSE, Keepalive alle 15 s |
| POST | `/v1/speak` | ja | Event erzeugen |
| POST | `/v1/ack` | ja | Gerät meldet Status (`id`, `device`, `status`) |
| POST | `/v1/mute` | ja | `{scope: all\|session\|device\|group, id, muted}` |
| GET | `/v1/state` | ja | Geräte, Verbindung, Mutes, letzte 20 Events |

## Sicherheit
- Bearer-Token, Vergleich mit `hmac.compare_digest`; Token ≥ 16 Zeichen; mehrere Tokens zur Rotation.
- Ohne Token startet das Relay nicht. `LOCAL_UNSAFE_NO_AUTH=true` nur auf Loopback, sonst Abbruch (Exit 2).
- Keine Tokens in Logs oder URLs (nur Header). Relay loggt keine Header.
- Body-Limit 64 KiB, Text-Limit 4000 Zeichen. TLS terminiert Pangolin/Reverse Proxy.
- Der Hook bereinigt Code, URLs und Tabellen vor dem Senden und spricht max. `CLAUDE_VOICE_MAX_CHARS`.
  Es werden keine Dateiinhalte oder Tool-Outputs gesendet, nur Assistant-Text.

## Entscheidungen
| Thema | Entscheidung | Grund |
|---|---|---|
| Transport | Pull per SSE | Notebook oft zu, NAT-freundlich, kein Fan-out im Hook |
| Fan-out | Relay erzeugt pro Gerät ein eigenes Event | Eigenes Ack/TTL, Teilausfall blockiert nichts |
| Veraltete Events | TTL, danach verworfen | Nichts 10 Minuten später vorlesen |
| Default-Routing | `active` (jüngste Verbindung) | Kein Echo auf allen Geräten |
| Stack | Python stdlib | Null Abhängigkeiten, läuft überall |

## Roadmap
- **v0.2 Audio-Fan-out:** TTS-Worker im Relay rendert satzweise einmal, verteilt kurzlebige Audio-URLs → HA (`media_player.play_media`/`tts.speak`) und Smart TV. Gleiche Stimme, 1× TTS-Last.
- **v0.3 Synchronität:** `play_at` (NTP) + `latency_offset_ms` pro Gerät auswerten.
- **Presence:** `home` nur bei Anwesenheit (HA), sonst Fallback `active`.
- **Barge-in:** neues Event bricht laufende Wiedergabe derselben Session ab.
- Claude-Code-Cloud-Environment: Plugin per Start-Script ziehen, Env-Variablen im Environment setzen.
