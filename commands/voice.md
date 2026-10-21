---
description: Voice output control (status | mute | unmute | speak <text> | update [ref] | version)
argument-hint: status | mute | unmute | speak <text> | update [ref] | version
allowed-tools: Bash(python3:*)
---
Run voicectl for the user's request: `$ARGUMENTS`

- `status` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" status`
- `mute` / `unmute` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" local mute|unmute` (instant, client-side)
- `speak <text>` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" speak "<text>"`

- `update [ref]` -> `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/self_update.py" update` (with a ref: `update --ref <ref>`; default ref is `CLAUDE_VOICE_REF`)
- `version` -> `python3 "${CLAUDE_PLUGIN_ROOT}/hooks/self_update.py" version`

Report the result in one line. Never print the token.
