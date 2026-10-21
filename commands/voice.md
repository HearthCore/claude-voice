---
description: Voice output control (status | mute | unmute | speak <text>)
argument-hint: status | mute | unmute | speak <text>
allowed-tools: Bash(python3:*)
---
Run voicectl for the user's request: `$ARGUMENTS`

- `status` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" status`
- `mute` / `unmute` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" local mute|unmute` (instant, client-side)
- `speak <text>` -> `python3 "${CLAUDE_PLUGIN_ROOT}/tools/voicectl.py" speak "<text>"`

Report the result in one line. Never print the token.
