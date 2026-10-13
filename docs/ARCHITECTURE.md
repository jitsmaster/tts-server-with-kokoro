# Architecture: how Claude Code gets its voice

Status as of 2026-10-13. This describes what actually runs on the maintainer's machine; every number below was
measured there (RTX 3050 Ti 4 GB, i7-11850H, Windows 11).

## The path of one spoken message

```
Claude Code hook event (Stop / Notification / PermissionRequest / Elicitation / AskUserQuestion)
   |  JSON on stdin
   v
tts-send.exe <speak|stop|elicit>          native C client, ~35 ms, no console, exits after the ack
   |  loopback TCP 127.0.0.1:8882   frame = int32 length + "mode\npayload", ack = 'K'   (the player also listens on
   |  named pipe \\.\pipe\tts-hook-daemon-v1 for its own client and --quit)
   v
tts-hook.exe --daemon  (the resident player; starts itself on demand, one instance per user)
   |  1. build the spoken text (clean markdown, skill name, session name, Insights section)
   |  2. cache hit?  -> play at once
   |  3. else POST http://127.0.0.1:8880/tts/stream   (frames: int32 length + WAV; length 0 = end)
   |  4. queue the message as soon as its FIRST phrase arrives; later phrases keep arriving while it plays
   v
playback thread: questions first, then FIFO; one machine-wide mutex; phrases handed over 0.15 s early
   v
System.Media.SoundPlayer  ->  speakers          (stop: tts-stop.flag, polled every 15-50 ms)

Kokoro TTS server (FastAPI, GPU)  <-  /tts (whole WAV)   /tts/stream (phrase by phrase)
   English: pipeline 'b' (British), voices bm_daniel / bf_isabella         v1.0 weights
   Chinese: own model hexgrad/Kokoro-82M-v1.1-zh, voices zm_010 / zf_001   (any Chinese character => Chinese)
```

## Why it looks like this (what the measurements said)

| Finding | Consequence |
|---|---|
| Model speed is ~10x real time (about 1 s per 10 s of audio); G2P is ~3 ms | the model was never the main delay |
| Each `powershell.exe` start costs ~1 s; the old chain started two | replaced by a native client and one resident player |
| The PowerShell skill lookup took 2.3 s on a 0.6 MB transcript, 11 s on 1.5 MB, 29 s on 13.6 MB (it parsed up to 400 huge JSON lines) | the player scans the transcript tail as text and only parses lines that mention the Skill tool (~20 ms) |
| The first audio waited for the whole message to be synthesized (1.4-1.7 s for 14 s of speech) | streaming: first phrase ready after 170-220 ms server-side |
| A .NET Framework exe needs ~110-150 ms just to start here | the hook command is a C program |
| Lowering the sample rate does not speed generation (the model computes at 24 kHz) | not done |

## Components

| File | Role |
|---|---|
| `app/main.py` | FastAPI: `POST /tts` (whole WAV) and `POST /tts/stream` (frames) |
| `app/tts_engine.py` | pipelines, Chinese model loading, `synthesize`, `synthesize_stream`, `chunk_text` |
| `app/language_detect.py` | any CJK ideograph => `zh`, else `en` |
| `app/voices.py` | voice pools per language and gender |
| `scripts/examples/claude-code-hooks/tts-send.c` | the hook command (gcc: `gcc -O2 -s -mwindows -o tts-send.exe tts-send.c -lws2_32`) |
| `scripts/examples/claude-code-hooks/tts-hook.cs` | the resident player, plus a one-shot fallback and `--dry`/`--quit` (csc, see below) |
| `scripts/examples/claude-code-hooks/tts-stop-hotkey.cs` | Ctrl+Alt+M listener (separate scheduled task `TtsStopHotkey`) |
| the old `*.ps1` hook scripts | untouched, kept for rollback |

## Server

- `POST /tts`: `{text, voice_gender?, speed?, pause_s?}` -> one `audio/wav`. Unchanged; other tools use it.
- `POST /tts/stream`: same request. Response `application/x-wav-frames`: repeated `[uint32 LE length][complete 24 kHz mono WAV]`,
  ended by a zero length. A stream that stops without the zero length was cut short by an error. `pause_s` here sets
  the silence appended to each phrase (default 0.25 s). The first phrase is synthesized before the headers go out,
  so a failure there is a clean HTTP 500.
- Phrases (`chunk_text`): sentences; sentences over 240 "weight" are cut at commas, then spaces; phrases under 20
  are glued onto the next. Weight counts a Chinese character as 3 (`len + 2 * CJK count`). Each phrase has the
  model's silent padding trimmed (keeping 50 ms) and 0.25 s of silence appended. The inference lock is held per
  phrase, so a question can interleave with a long message already being synthesized.
- Language routing: any Chinese character sends the whole text to the Chinese model; Latin words inside it are
  phonemized by misaki's English G2P (no English TTS model) and spoken by the Chinese voice. Pinned kokoro 0.7.16
  has no `repo_id`, so `tts_engine.py` downloads the v1.1-zh config, weights and voices with `hf_hub_download`
  and passes a `KModel` into `KPipeline`. If that fails at startup, English still works and the next Chinese
  request retries.
- Speed: `speed` multiplies the predicted durations; it shortens speech less than linearly (171 characters:
  10.0 s at 1.0, 8.6 s at 1.25, 7.3 s at 1.35).

## Player (tts-hook.exe)

- **One instance.** Mutex `Global\TtsHookDaemon`. `tts-send` starts it (with breakaway from Claude Code's job
  object, working directory `.claude`) when the mutex does not exist; if that fails it hands the payload to a one-shot
  `tts-hook.exe <mode> --worker <file>` (that last-resort path in the C client is not covered by tests).
- **Pipe security.** Only the current user can open the pipe. The TCP port is bound to 127.0.0.1 only but has no
  per-user check: any local process can send `speak`/`stop`/`elicit` frames (it cannot run code or read anything).
- **Concurrency.** One thread per request builds text and talks to the server; one playback thread plays.
- **Queue.** An item is queued when its first chunk exists. Questions (`elicit`) go first, otherwise FIFO. An item
  that waited over 180 s is dropped (the old design dropped anything that waited 30 s for the mutex).
- **Playback.** Chunks are complete WAVs played with `SoundPlayer`. The next chunk starts at
  `max(duration - 0.15 s, duration / 2)`, i.e. inside the 0.25 s silent pad, so sentences are ~0.11 s apart
  (log: `gaps=[-136,-129,...]` means the next chunk started that many ms before the previous one's natural end).
  The mutex `Global\KokoroTtsPlayback` is held for the whole message and is shared with one-shot workers and with
  other projects' hooks that still use PowerShell.
- **Stop.** The hotkey writes `tts-stop.flag` and kills the PID in `audio.pids`. The player polls the flag, stops
  the current chunk, cancels the item (the stream reader stops) and never writes its own PID to `audio.pids`
  (the hotkey would kill it). A stale flag is cleared before the next message.
- **Cache.** Texts up to 200 characters are cached as one WAV in `.claude\tts-cache\` (key = SHA1 of gender, speed
  and text; at most 300 files, oldest removed). A streamed short message is reassembled and cached when the stream
  ended cleanly. The key does not include the model or voice: clear the folder after changing either.
- **Fallbacks.** Streaming unavailable (old server, `"streaming": false`) -> whole-message `/tts`. Stream cut after
  the first phrase -> what arrived is played and the log line carries `note=stream-cut`.

## State and configuration (all under `C:\Users\awang\.claude\`)

| File | Meaning |
|---|---|
| `tts-config.json` | `speed` (0.5-1.5, omit or 1.0 = default), `cacheMaxChars` (200), `cacheMaxFiles` (300), `streaming` (true) |
| `tts-muted.flag` | present = silent (toggle with the `toggle-voice` skill) |
| `tts-stop.flag`, `audio.pids`, `tts-priority-pending.flag` | stop and one-shot-worker coordination |
| `tts-hook.log` | one line per message: mode, `[player]`/`[worker]`, ok/FAILED, characters, stage timings (`first=` ms until the first phrase, `stream=` ms until the last, `qwait=`), chunks and gaps; also `send[...] SLOW` lines from the C client |
| `tts-hook-errors.log`, `tts-client-errors.log`, `tts-failures.log` | unexpected exceptions, HTTP/mutex errors, texts that failed |
| `settings.json` | hook commands: `C:/Users/awang/.claude/tts-send.exe speak\|stop\|elicit` (`async: true`) |

Hook mapping: `Notification`, `PermissionRequest` -> `speak` (male); `Stop` -> `stop` (male; adds skill name, session name
and a spoken Insights section); `Elicitation`, `PreToolUse` on `AskUserQuestion` -> `elicit` (female, high priority).

## Measured results

| | before | now |
|---|---|---|
| hook start to first sound, long Stop message | 2 x ~1 s PowerShell + skill lookup 2-29 s + whole synthesis ~1.5 s | client ~100 ms (through cmd.exe), lookup ~20 ms, first phrase 0.4-0.6 s |
| repeated short phrase | full chain | cache hit, playing 3-5 ms after the player receives it |
| 311-character message (4 phrases) | whole synthesis then play | `first=425ms stream=1118ms`, gaps ~ -136 ms |
| Chinese, 3 phrases | one 1.4-2.5 s wait | first phrase 0.6-1.7 s (the slow figure was the first request after a server restart) |

## Build, deploy, restart

```powershell
# player (C# 5 compiler from .NET Framework)
tts-hook.exe --quit      # the exe is locked while the player runs
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:winexe /optimize /out:C:\Users\awang\.claude\tts-hook.exe /reference:System.Web.Extensions.dll tts-hook.cs
# hook client
gcc -O2 -s -mwindows -o C:\Users\awang\.claude\tts-send.exe tts-send.c -lws2_32
# debug what would be said, no sound
Get-Content payload.json | tts-hook.exe stop --dry
```

The next hook starts a fresh player. Server: kill both `python.exe ... uvicorn` processes, then run
`start-hidden.ps1` (about 30 s until it answers). The `KokoroTtsServer` scheduled task (logon trigger, runs `start-hidden.ps1`, which does nothing if port 8880 is already
listening) is enabled.
Tests: `.\.venv\Scripts\python.exe -m pytest -q` (35 pass).

## Gotchas

- **The server cannot cancel a request.** A 224,000-character test text wedged it (the client timed out at 30 s, the
  server kept synthesizing); recovery was killing uvicorn and restarting. Streaming limits the damage of long texts
  (a stop cancels the rest), but never test with huge texts.
- The player once locked the working directory of whichever project's hook started it; it now starts in `.claude`.
- Messages that wait behind a long one are no longer dropped after 30 s; they wait up to 180 s.
- Other projects' hooks (e.g. dsh-plugins) still run PowerShell, write `audio.pids` and share the mutex; they are not sped up.
- **Slow `tts-send` returns (solved).** 3 of ~25 sends took 1.9-9.5 s. Cause: opening the named pipe from a freshly
  started process stalls for 0.5-9 s at random on this machine (cause unknown: minifilter/EDR suspected). A probe
  exe measured pipe open 0-2600 ms against loopback TCP connect 0-16 ms and a plain file open 0-16 ms, sequential or
  concurrent, and a pure Node pipe server showed the same stalls, so it is not the player. Fix: `tts-send` uses
  loopback TCP (port 8882). After the change 12 warm sends and 30 concurrent sends all returned in 40-250 ms
  (Node-measured, including process start); none was logged as SLOW. Two smaller fixes along the way: the player now
  runs three pipe listeners (with one, a client arriving between connections got "no such pipe" about 0.6% of the
  time and started a useless second player), and a blocking connect to a closed port takes ~2 s on Windows, so the
  client checks the player mutex first and uses 200 ms non-blocking connect attempts.
  A cold start (no player) costs ~0.7 s once. `tts-send` logs `send[...] SLOW total= stdin= connect= ack=` to
  `tts-hook.log` for any send over 250 ms.

## Rollback

`settings.json.bak-20261013-tts-send` (hooks call `tts-hook.exe` directly) and `settings.json.bak-20261013-tts-hook`
(the original PowerShell chain), both next to `settings.json`. Set `"streaming": false` to return to whole-message
synthesis without touching hooks.
