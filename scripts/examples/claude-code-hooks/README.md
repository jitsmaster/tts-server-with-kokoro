# Claude Code hook scripts (verbatim)

These are the actual production PowerShell hook scripts registered in
Claude Code's `~/.claude/settings.json` on the maintainer's machine, copied
here verbatim as a real-world reference for calling this server. They're
**not meant to be run as-is from this repo** — they hardcode
`C:\Users\awang\.claude\...` paths for logs/flags and depend on each other
via dot-sourcing. For a self-contained, portable starting point, see the
trimmed examples one directory up instead.

| File | Claude Code hook event | Voice |
|---|---|---|
| `speak.ps1` | `PermissionRequest`, `Notification` | male |
| `stop-speak.ps1` | `Stop` (task completion) | male |
| `elicitation-speak.ps1` | `Elicitation`, `PreToolUse` on `AskUserQuestion` (questions) | female, high priority |
| `tts-client.ps1` | dot-sourced by all three — the actual `POST /tts` call, playback mutex, and priority-queue logic | — |
| `skill-lookup.ps1` | dot-sourced by all three — reads the session transcript to announce which skill/session is talking | — |
| `detach-launcher.ps1` | wraps the above so Claude Code's hook call returns immediately while speech plays in a detached background process | — |

Each hook receives the Claude Code hook payload as JSON on stdin (e.g.
`{"message": "...", "transcript_path": "..."}` or, for `AskUserQuestion`,
`{"tool_input": {"questions": [...]}}`), extracts the text to speak, strips
markdown, and calls `Invoke-KokoroTts -Text ... -Gender ...` from
`tts-client.ps1`.
