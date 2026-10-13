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
| `tts-hook.cs` | **replaces all of the PowerShell rows above** in the live setup. One compiled exe that is both the resident audio player (`tts-hook.exe --daemon`, auto-started, listens on named pipe `tts-hook-daemon-v1`) and a fallback one-shot client. The player builds the spoken text, finds the last skill by scanning the transcript tail as text (PowerShell took 2-29 s on 1-14 MB transcripts), caches short phrases in `tts-cache\`, streams each message from `/tts/stream` and starts playing after the first phrase, synthesizes requests in parallel and plays them from one queue (questions first, then FIFO; nothing dropped for 180 s), keeps config, mute/stop flags, session names, the cache index and recent WAVs in memory (file watchers on `tts-*` and `sessions\*.json`; the skill lookup scans the transcript backwards and stops at the first hit) instead of re-reading files per message, reads `tts-config.json` (`speed`, `cacheMaxChars`, `cacheMaxFiles`, `streaming`) and does not put its PID in `audio.pids` (the hotkey would kill it). `tts-hook.exe --quit` stops it (do that before rebuilding). Build: `csc /target:winexe /optimize /out:tts-hook.exe /reference:System.Web.Extensions.dll tts-hook.cs`. Debug: `tts-hook.exe stop --dry < payload.json` prints the spoken text. | male / female |
| `tts-send.c` | the hook command itself: an 18 KB native client (~35 ms, vs ~150 ms for a .NET exe and ~1 s for powershell.exe) that sends the hook JSON to the player over loopback TCP (127.0.0.1:8882; a named pipe opened from a fresh process stalled for seconds at random) and exits; starts the player if it is not running. Build: `gcc -O2 -s -mwindows -o tts-send.exe tts-send.c -lws2_32`. Hook command: `tts-send.exe speak\|stop\|elicit`. | - |
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
