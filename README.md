# Kokoro TTS Server

Local FastAPI + Kokoro TTS server for English/Chinese, 1 male + 1 female voice
each. Used by Claude Code's PowerShell voice hooks.

## Setup

1. Install espeak-ng (needed for English phonemization):
   ```powershell
   winget install eSpeak-NG.eSpeak-NG
   ```
   If winget isn't available, download the installer from
   https://github.com/espeak-ng/espeak-ng/releases and run it manually.

2. Create the venv and install dependencies:
   ```powershell
   python -m venv .venv
   .venv\Scripts\pip.exe install -r requirements.txt
   ```

   `requirements.txt` pins `torch==2.6.0+cu124` and points at PyTorch's CUDA
   wheel index, so this installs the GPU build directly — no separate
   `--index-url` step needed. A plain `pip install torch` (e.g. if adding a
   package by hand later) defaults to CPU-only on Windows and will silently
   make synthesis much slower; if the CUDA version here ever falls behind
   the driver (check with `nvidia-smi`), update the pin and the
   `--extra-index-url` line together.

3. Start the server:
   ```powershell
   .\start.ps1
   ```

   The server listens on `http://127.0.0.1:8880`. On startup it prints which
   device (`cuda` or `cpu`) Kokoro loaded on — check that output if audio
   generation seems slow; `cpu` there means the GPU build didn't install
   correctly.

## Windows Startup

The hooks assume the server is already running, so it's registered to start
automatically at logon via a Scheduled Task rather than needing `start.ps1`
run by hand every session.

- **Task name:** `KokoroTtsServer`, trigger: at logon of the current user.
- **Action:** runs `start-hidden.ps1` (in this folder) hidden, which launches
  the same `uvicorn` command as `start.ps1` via `Start-Process`, with stdout/
  stderr redirected to `logs\server.log` / `logs\server.error.log` (gitignored)
  so a boot-time failure is still diagnosable.
- To (re-)register it, from this folder:
  ```powershell
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -File `"$PWD\start-hidden.ps1`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  Register-ScheduledTask -TaskName "KokoroTtsServer" -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Starts the local Kokoro TTS server for Claude Code voice hooks at logon." -Force
  ```
- To check it manually: `Start-ScheduledTask -TaskName "KokoroTtsServer"` then
  `Get-ScheduledTaskInfo -TaskName "KokoroTtsServer"` for `LastTaskResult` (`0`
  = success).

**Caveat:** the task's `-File` path currently points at this feature branch's
git worktree location. Once this branch merges into `main` and the worktree
is removed, re-run the registration command above from wherever the code then
lives (e.g. the main repo root) — the old path will otherwise silently stop
existing and the task will fail at next logon.

## API

`POST /tts`
```json
{ "text": "Hello there", "voice_gender": "male" }
```
`voice_gender` is optional (`"male"` or `"female"`); if omitted, one is chosen
at random. Returns `audio/wav` bytes.

## Voices

| Language | Male | Female |
|---|---|---|
| English | am_michael | af_bella |
| Chinese | zm_yunjian | zf_xiaobei |
