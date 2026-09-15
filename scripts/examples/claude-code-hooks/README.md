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
| `tts-stop-hotkey.cs` | not a hook — a persistent global-hotkey listener (Ctrl+Alt+M) that instantly stops whatever is currently playing | — |

Each hook receives the Claude Code hook payload as JSON on stdin (e.g.
`{"message": "...", "transcript_path": "..."}` or, for `AskUserQuestion`,
`{"tool_input": {"questions": [...]}}`), extracts the text to speak, strips
markdown, and calls `Invoke-KokoroTts -Text ... -Gender ...` from
`tts-client.ps1`.

## Stopping playback instantly (`tts-stop-hotkey.cs`)

`tts-client.ps1` writes the currently-playing process's PID to `audio.pids`
and polls `tts-stop.flag` every 50ms during playback, so anything that writes
that flag and/or kills that PID stops audio almost immediately (measured
~150-500ms end to end).

The first version of this bound Ctrl+Alt+M via a **Desktop shortcut's
"Shortcut key" property**. That turned out to be the actual bottleneck, not
the playback code: Explorer owns that mechanism, and it took **5+ seconds**
(measured with a real simulated keypress) just to get around to launching the
shortcut's target process. There's no way to make Explorer's own shortcut-key
dispatch faster — it just isn't a low-latency mechanism.

The fix is `tts-stop-hotkey.cs`: a tiny persistent WinForms app that calls
`RegisterHotKey` for Ctrl+Alt+M directly and reacts to `WM_HOTKEY` in its own
message loop — no Explorer, no process spawn, just an already-running process
getting a message. On hotkey it writes the stop flag *and* hard-kills the
recorded PID (belt-and-suspenders: the kill guarantees silence even if a
script is stuck in the blocking `PlaySync()` fallback path, which doesn't
poll the flag at all).

Gotcha hit while building this: the first attempt overrode
`SetVisibleCore(false)` to keep the window invisible, which silently
prevented `OnLoad` (and therefore `RegisterHotKey`) from ever running — the
process looked alive but the hotkey was never actually registered. The fix
that keeps `OnLoad` firing normally while staying invisible: don't override
visibility at all, just position the form off-screen with zero size and zero
opacity (`ShowInTaskbar = false`, `Location = (-2000, -2000)`, `Size = (1,
1)`, `Opacity = 0`).

To build and run it (paths in the source are hardcoded to this repo's
maintainer's machine — change `FlagPath`/`PidsPath` for your own setup):

```powershell
csc /target:winexe /out:tts-stop-hotkey.exe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll tts-stop-hotkey.cs
```

Then register it to start at logon (same pattern as the server's own
`KokoroTtsServer` task in the top-level README) so it's always running:

```powershell
$action = New-ScheduledTaskAction -Execute "C:\path\to\tts-stop-hotkey.exe"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "TtsStopHotkey" -Action $action -Trigger $trigger -Principal $principal -Description "Global Ctrl+Alt+M hotkey to instantly stop Kokoro TTS playback."
```
