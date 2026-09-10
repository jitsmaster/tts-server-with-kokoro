# Kokoro TTS Server + Claude Code Voice Hooks — Design

## Goal

Replace the Windows `System.Speech` (SAPI) narration currently used by Claude Code's
PowerShell hook scripts with a local, GPU-backed Kokoro TTS server, supporting English
and Chinese with distinct male/female voice pools, while keeping the existing OS voice
as an automatic fallback if the server is unavailable.

## Reference

`https://github.com/jitsmaster/novel-tts/tree/main/tts-server` — Chinese-only FastAPI +
Kokoro server. Used only as a structural reference (project layout, streaming response
pattern); not reused as code since it doesn't support English or multi-voice pools.

## Out of scope

- Auto-starting the server (Windows service / Task Scheduler) — v1 is manual start.
- Automated test suite requiring GPU hardware.
- Any language other than English/Chinese.
- Changing the mp3 chime playback (`done.mp3`, `question.mp3`, `myturn.mp3`) or the
  skill-name-lookup / markdown-stripping logic in the hook scripts.

## Part 1 — FastAPI TTS Server

**Location:** `D:\dev\TTS\tts-server-with-kokoro`

**Runtime:** Python (existing 3.13 install), FastAPI + `uvicorn`, `kokoro` pip package
(PyTorch-based `KPipeline`, not `kokoro-onnx`) running on CUDA when available, falling
back to CPU if no GPU is detected at startup. `espeak-ng` installed on the host for
English out-of-vocabulary phonemization (Chinese phonemization does not need it).

### Voice table (`app/voices.py`)

```python
VOICES = {
    "en": {"male": ["am_adam", "am_michael"], "female": ["af_heart", "af_bella"]},
    "zh": {"male": ["zm_yunxi", "zm_yunjian"], "female": ["zf_xiaobei", "zf_xiaoni"]},
}
```

### Language detection (`app/language_detect.py`)

Character-range analysis, not `langdetect`: count Unicode code points in the CJK
ranges (`\u4e00-\u9fff` plus common extensions) vs. Latin letters (`a-zA-Z`) in the
input text. Whichever count is higher wins; ties default to English. This is more
reliable than statistical detectors on the short strings (assistant messages, question
prompts) this server actually receives.

### TTS engine (`app/tts_engine.py`)

- One `KPipeline` instance per language code (`'a'` = American English, `'z'` =
  Mandarin), constructed once at process startup (not per-request) and kept in memory
  — this is the expensive GPU load.
- A `synthesize(text: str, voice_id: str) -> bytes` function that runs the pipeline
  and returns a WAV byte buffer (24kHz mono, Kokoro's native output rate).

### API (`app/main.py`)

`POST /tts`

Request body:
```json
{ "text": "string, required", "voice_gender": "male | female, optional" }
```

Behavior:
1. `text` empty/whitespace-only → `400` with a JSON error message.
2. Detect language via `language_detect`.
3. Resolve gender: use `voice_gender` if provided and valid; otherwise pick randomly
   between `male`/`female`.
4. Randomly select one voice ID from `VOICES[language][gender]`.
5. Synthesize; on any Kokoro/synthesis exception, return `500` with a JSON error
   message.
6. Return the WAV bytes as `StreamingResponse` with `media_type="audio/wav"`.

**Port:** `8880` (matches the `kokoro-fastapi` project convention, avoids clashing with
common dev ports).

### Files

```
tts-server-with-kokoro/
  app/
    __init__.py
    main.py
    voices.py
    language_detect.py
    tts_engine.py
  requirements.txt
  start.ps1
  README.md
```

`start.ps1` activates the project's venv and runs
`uvicorn app.main:app --host 127.0.0.1 --port 8880`.

`README.md` documents: creating the venv, installing `requirements.txt`, installing
`espeak-ng` (winget/choco command plus a manual-download fallback), and running
`start.ps1`.

## Part 2 — PowerShell Hook Integration

**New shared file:** `C:\Users\awang\.claude\tts-client.ps1`, dot-sourced by each hook
script, providing:

```powershell
function Invoke-KokoroTts {
    param([string]$Text, [string]$Gender)
    # POST to http://127.0.0.1:8880/tts with a ~3s timeout.
    # On success: save WAV bytes to a temp file, play via
    # System.Windows.Media.MediaPlayer, return $true.
    # On any exception (timeout, connection refused, non-200): return $false,
    # caller falls back to System.Speech.
}
```

### Per-script changes

| Script | Event | Kokoro gender | OS-voice fallback gender | Change from today |
|---|---|---|---|---|
| `elicitation-speak.ps1` | Question/prompt (AskUserQuestion) | male | **male** | fallback gender flips female→male to match the new rule |
| `stop-speak.ps1` | Completion/response (Stop hook) | female | female | unchanged fallback |
| `speak.ps1` (via `wf-speak-notification.ps1` and inline Notification hook) | Generic notification | female | female | unchanged fallback |

Each script's flow becomes: check mute flag (unchanged) → build/clean `$msg`
(unchanged) → try `Invoke-KokoroTts -Text $msg -Gender <table above>` → if it returns
`$false`, run the existing `System.Speech` `SpeechSynthesizer`/SSML block exactly as
today (with the one gender fix noted above) → play the mp3 chime (unchanged).

Randomization of which of the two voices in a gender pool gets used happens
server-side (Part 1, step 4) — the PowerShell layer only ever sends `Gender`, never a
specific voice ID, so repeated calls naturally vary.

## Error handling summary

- Empty `text` reaching the server → `400`, but this should never happen in practice
  since PowerShell only calls it with a non-empty cleaned message.
- Server unreachable, timeout, or non-2xx response → PowerShell catches it and falls
  back to `System.Speech`; no error surfaced to the user (per confirmed requirement).
- Kokoro synthesis exception → server-side `500`, treated the same as "unreachable" by
  the PowerShell client.

## Testing plan

No GPU-dependent automated suite (YAGNI for a personal tool). Manual verification:
1. Start the server; `Invoke-RestMethod`/`curl` `POST /tts` with English text, no
   `voice_gender` — confirm playable WAV comes back.
2. Repeat with Chinese text, and with explicit `voice_gender=male` and `=female` for
   each language — confirm the right voice pool is used (spot-check by ear).
3. Empty text → confirm `400`.
4. Trigger a real `AskUserQuestion` in Claude Code with the server running — confirm
   male Kokoro voice plays.
5. Let a turn complete (Stop hook) with the server running — confirm female Kokoro
   voice plays.
6. Stop the server and repeat steps 4–5 — confirm silent fallback to the (now
   gender-corrected) OS voice, no visible errors.
