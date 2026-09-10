# Kokoro TTS Server + Claude Code Voice Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a local GPU-backed FastAPI + Kokoro TTS server with English/Chinese, male/female voice pools, and wire Claude Code's PowerShell voice hooks to use it, falling back to the existing OS TTS on any failure.

**Architecture:** A FastAPI app (`app/main.py`) loads two Kokoro `KPipeline` instances (English, Mandarin) once at startup, exposes `POST /tts` which detects language by character-range analysis, randomly picks a voice from a static gender-keyed voice table, synthesizes WAV bytes, and streams them back. Claude Code's hook scripts (`C:\Users\awang\.claude\*.ps1`) call this endpoint via a shared helper function with a short timeout, falling back to today's `System.Speech` code path unchanged (aside from one gender fix) if the server doesn't respond.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, `kokoro` (PyTorch-based `KPipeline`), `soundfile`, `torch` (CUDA if available), pytest + httpx for server tests, PowerShell 5.1 for hook scripts.

**Spec:** `D:\dev\TTS\tts-server-with-kokoro\docs\superpowers\specs\2026-09-10-kokoro-tts-server-design.md`

## Global Constraints

- Server listens on `127.0.0.1:8880` (fixed port, matches spec).
- Voice table is exactly: `en.male=[am_adam, am_michael]`, `en.female=[af_heart, af_bella]`, `zh.male=[zm_yunxi, zm_yunjian]`, `zh.female=[zf_xiaobei, zf_xiaoni]`.
- Language detection is character-range analysis (CJK vs Latin counts), not `langdetect`.
- Use the `kokoro` PyTorch pipeline package, not `kokoro-onnx`.
- `espeak-ng` must be installed on the host for English phonemization; document this in `README.md`.
- No auto-start / Windows service for v1 — server is started manually via `start.ps1`.
- No automated GPU-dependent test suite — server tests mock `synthesize()`; Kokoro-engine and PowerShell verification are manual steps with exact commands to run.
- Every PowerShell hook change must fall back silently (no visible error) to the existing `System.Speech` path on any Kokoro-call failure, using a ~3 second timeout.
- All shell commands in this plan run via the **PowerShell tool**, never Bash (Windows environment rule).
- Commit after each task, per the repo's existing commit style (this repo has one commit so far: `docs: add Kokoro TTS server design spec`).

---

### Task 1: Project scaffolding, dependencies, and voice table

**Files:**
- Create: `D:\dev\TTS\tts-server-with-kokoro\requirements.txt`
- Create: `D:\dev\TTS\tts-server-with-kokoro\app\__init__.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\app\voices.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\tests\__init__.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\tests\test_voices.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\.gitignore`

**Interfaces:**
- Produces: `VOICES: dict[str, dict[str, list[str]]]` and `pick_voice(language: str, gender: str) -> str` in `app/voices.py`, used by Task 5 (`main.py`).

- [ ] **Step 1: Create the project skeleton and `.gitignore`**

```powershell
New-Item -ItemType Directory -Force -Path "D:\dev\TTS\tts-server-with-kokoro\app" | Out-Null
New-Item -ItemType Directory -Force -Path "D:\dev\TTS\tts-server-with-kokoro\tests" | Out-Null
New-Item -ItemType File -Force -Path "D:\dev\TTS\tts-server-with-kokoro\app\__init__.py" | Out-Null
New-Item -ItemType File -Force -Path "D:\dev\TTS\tts-server-with-kokoro\tests\__init__.py" | Out-Null
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 2: Write `requirements.txt`**

```
fastapi
uvicorn[standard]
kokoro>=0.9.4
torch
soundfile
pytest
httpx
```

- [ ] **Step 3: Create the venv and install dependencies**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
python -m venv .venv
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\pip.exe" install -r requirements.txt
```

Expected: install completes without errors. If `torch` installs a CPU-only wheel and you have an NVIDIA GPU, note it — Task 4's manual verification step covers confirming CUDA is actually used.

- [ ] **Step 4: Write `app/voices.py`**

```python
import random

VOICES: dict[str, dict[str, list[str]]] = {
    "en": {"male": ["am_adam", "am_michael"], "female": ["af_heart", "af_bella"]},
    "zh": {"male": ["zm_yunxi", "zm_yunjian"], "female": ["zf_xiaobei", "zf_xiaoni"]},
}


def pick_voice(language: str, gender: str) -> str:
    """Randomly select one voice id from VOICES[language][gender]."""
    return random.choice(VOICES[language][gender])
```

- [ ] **Step 5: Write the failing test**

`tests/test_voices.py`:
```python
from unittest.mock import patch

from app.voices import VOICES, pick_voice


def test_pick_voice_returns_id_from_correct_pool():
    for language in VOICES:
        for gender in VOICES[language]:
            voice_id = pick_voice(language, gender)
            assert voice_id in VOICES[language][gender]


def test_pick_voice_uses_random_choice():
    with patch("app.voices.random.choice", return_value="am_adam") as mock_choice:
        result = pick_voice("en", "male")
        mock_choice.assert_called_once_with(["am_adam", "am_michael"])
        assert result == "am_adam"
```

- [ ] **Step 6: Run the test to verify it passes**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
& ".venv\Scripts\python.exe" -m pytest tests/test_voices.py -v
```

Expected: both tests `PASS`. (`app/voices.py` already exists from Step 4, so this confirms correctness rather than TDD red/green — the module is trivial enough to write directly, per the plan's task-sizing rule.)

- [ ] **Step 7: Commit**

```powershell
git add requirements.txt .gitignore app/__init__.py app/voices.py tests/__init__.py tests/test_voices.py
git commit -m "feat: scaffold TTS server project and voice table"
```

---

### Task 2: Language detection

**Files:**
- Create: `D:\dev\TTS\tts-server-with-kokoro\app\language_detect.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\tests\test_language_detect.py`

**Interfaces:**
- Consumes: nothing (pure function).
- Produces: `detect_language(text: str) -> str` (returns `"en"` or `"zh"`), used by Task 5 (`main.py`).

- [ ] **Step 1: Write the failing test**

`tests/test_language_detect.py`:
```python
from app.language_detect import detect_language


def test_pure_english_is_en():
    assert detect_language("How is the weather today?") == "en"


def test_pure_chinese_is_zh():
    assert detect_language("今天天气怎么样") == "zh"


def test_majority_chinese_with_some_latin_is_zh():
    assert detect_language("这是一个测试 test") == "zh"


def test_majority_english_with_some_chinese_is_en():
    assert detect_language("This is a test of 测试") == "en"


def test_no_letters_defaults_to_en():
    assert detect_language("123 !!! ???") == "en"
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
& ".venv\Scripts\python.exe" -m pytest tests/test_language_detect.py -v
```

Expected: `FAIL` with `ModuleNotFoundError: No module named 'app.language_detect'`.

- [ ] **Step 3: Write the implementation**

`app/language_detect.py`:
```python
import re

_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"
)
_LATIN_PATTERN = re.compile(r"[a-zA-Z]")


def detect_language(text: str) -> str:
    """Return 'zh' if CJK characters outnumber Latin letters, else 'en'."""
    cjk_count = len(_CJK_PATTERN.findall(text))
    latin_count = len(_LATIN_PATTERN.findall(text))
    return "zh" if cjk_count > latin_count else "en"
```

- [ ] **Step 4: Run test to verify it passes**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
& ".venv\Scripts\python.exe" -m pytest tests/test_language_detect.py -v
```

Expected: all 5 tests `PASS`.

- [ ] **Step 5: Commit**

```powershell
git add app/language_detect.py tests/test_language_detect.py
git commit -m "feat: add majority-language detection by character range"
```

---

### Task 3: Kokoro TTS engine wrapper

**Files:**
- Create: `D:\dev\TTS\tts-server-with-kokoro\app\tts_engine.py`

**Interfaces:**
- Consumes: `kokoro.KPipeline`, `torch.cuda.is_available()`.
- Produces: `synthesize(text: str, voice_id: str, language: str) -> bytes` (WAV bytes, 24kHz mono), used by Task 5 (`main.py`).

No automated test here — it requires the actual Kokoro model and (ideally) a GPU, which is out of scope for the pytest suite per the spec. Task 4 is the manual verification of this module through the running server.

- [ ] **Step 1: Write `app/tts_engine.py`**

```python
import io

import soundfile as sf
import torch
from kokoro import KPipeline

_LANG_CODES = {"en": "a", "zh": "z"}
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_pipelines: dict[str, KPipeline] = {}


def _get_pipeline(language: str) -> KPipeline:
    if language not in _pipelines:
        lang_code = _LANG_CODES[language]
        _pipelines[language] = KPipeline(lang_code=lang_code, device=_DEVICE)
    return _pipelines[language]


def synthesize(text: str, voice_id: str, language: str) -> bytes:
    """Run Kokoro synthesis and return WAV bytes (24kHz mono)."""
    pipeline = _get_pipeline(language)
    audio_chunks = [audio for _, _, audio in pipeline(text, voice=voice_id)]
    audio = torch.cat(audio_chunks) if len(audio_chunks) > 1 else audio_chunks[0]

    buffer = io.BytesIO()
    sf.write(buffer, audio.numpy(), samplerate=24000, format="WAV")
    return buffer.getvalue()
```

- [ ] **Step 2: Commit**

```powershell
git add app/tts_engine.py
git commit -m "feat: add Kokoro pipeline wrapper for WAV synthesis"
```

(Verified for real in Task 4, once the FastAPI app can route a request into it.)

---

### Task 4: FastAPI `/tts` endpoint

**Files:**
- Create: `D:\dev\TTS\tts-server-with-kokoro\app\main.py`
- Create: `D:\dev\TTS\tts-server-with-kokoro\tests\test_main.py`

**Interfaces:**
- Consumes: `VOICES`, `pick_voice` from `app.voices`; `detect_language` from `app.language_detect`; `synthesize` from `app.tts_engine`.
- Produces: `app: FastAPI` instance with `POST /tts`, used by Task 5 (`start.ps1`) and by the PowerShell hooks (Task 7+) as an HTTP endpoint.

- [ ] **Step 1: Write the failing tests**

`tests/test_main.py`:
```python
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _fake_synthesize(text, voice_id, language):
    return b"RIFF....WAVEfake"


def test_empty_text_returns_400():
    response = client.post("/tts", json={"text": "   "})
    assert response.status_code == 400


def test_english_text_returns_wav_audio():
    with patch("app.main.synthesize", side_effect=_fake_synthesize):
        response = client.post("/tts", json={"text": "Hello there"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF....WAVEfake"


def test_chinese_text_with_explicit_gender_picks_zh_pool():
    captured = {}

    def capture_synthesize(text, voice_id, language):
        captured["voice_id"] = voice_id
        captured["language"] = language
        return b"fake"

    with patch("app.main.synthesize", side_effect=capture_synthesize):
        response = client.post(
            "/tts", json={"text": "你好世界", "voice_gender": "male"}
        )
    assert response.status_code == 200
    assert captured["language"] == "zh"
    assert captured["voice_id"] in ["zm_yunxi", "zm_yunjian"]


def test_invalid_gender_falls_back_to_random_choice():
    captured = {}

    def capture_synthesize(text, voice_id, language):
        captured["voice_id"] = voice_id
        return b"fake"

    with patch("app.main.synthesize", side_effect=capture_synthesize):
        response = client.post(
            "/tts", json={"text": "Hello", "voice_gender": "robot"}
        )
    assert response.status_code == 200
    assert captured["voice_id"] in ["am_adam", "am_michael", "af_heart", "af_bella"]


def test_synthesis_exception_returns_500():
    def raise_error(text, voice_id, language):
        raise RuntimeError("model exploded")

    with patch("app.main.synthesize", side_effect=raise_error):
        response = client.post("/tts", json={"text": "Hello"})
    assert response.status_code == 500
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
& ".venv\Scripts\python.exe" -m pytest tests/test_main.py -v
```

Expected: `FAIL` with `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Write the implementation**

`app/main.py`:
```python
import io
import random

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.language_detect import detect_language
from app.tts_engine import synthesize
from app.voices import pick_voice

app = FastAPI()


class TtsRequest(BaseModel):
    text: str
    voice_gender: str | None = None


@app.post("/tts")
def tts(req: TtsRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text must not be empty")

    language = detect_language(text)
    gender = req.voice_gender if req.voice_gender in ("male", "female") else random.choice(["male", "female"])
    voice_id = pick_voice(language, gender)

    try:
        audio_bytes = synthesize(text, voice_id, language)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return StreamingResponse(io.BytesIO(audio_bytes), media_type="audio/wav")
```

- [ ] **Step 4: Run tests to verify they pass**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
& ".venv\Scripts\python.exe" -m pytest tests/test_main.py -v
```

Expected: all 5 tests `PASS`.

- [ ] **Step 5: Commit**

```powershell
git add app/main.py tests/test_main.py
git commit -m "feat: add POST /tts endpoint with language and gender routing"
```

---

### Task 5: Startup script, README, and real Kokoro smoke test

**Files:**
- Create: `D:\dev\TTS\tts-server-with-kokoro\start.ps1`
- Create: `D:\dev\TTS\tts-server-with-kokoro\README.md`

**Interfaces:**
- Consumes: `app.main:app` (Task 4).
- Produces: a running server on `http://127.0.0.1:8880`, which Task 7's PowerShell client depends on.

- [ ] **Step 1: Write `start.ps1`**

```powershell
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$root\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8880 --app-dir $root
```

- [ ] **Step 2: Write `README.md`**

```markdown
# Kokoro TTS Server

Local FastAPI + Kokoro TTS server for English/Chinese, 2 male + 2 female voices
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

3. Start the server:
   ```powershell
   .\start.ps1
   ```

   The server listens on `http://127.0.0.1:8880`. It logs which device
   (`cuda` or `cpu`) it loaded Kokoro on — check the startup output if audio
   generation seems slow.

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
| English | am_adam, am_michael | af_heart, af_bella |
| Chinese | zm_yunxi, zm_yunjian | zf_xiaobei, zf_xiaoni |
```

- [ ] **Step 3: Install espeak-ng**

```powershell
winget install eSpeak-NG.eSpeak-NG
```

Expected: installs successfully. If `winget` isn't available or the package id has changed, download the installer from the espeak-ng GitHub releases page and run it manually, then continue.

- [ ] **Step 4: Start the server**

```powershell
cd "D:\dev\TTS\tts-server-with-kokoro"
.\start.ps1
```

Expected: uvicorn starts and logs `Uvicorn running on http://127.0.0.1:8880`. The first request will trigger a Kokoro model download (cached afterward) — this can take a minute or two. Leave this running in its own terminal for the next step.

- [ ] **Step 5: Manually verify English + Chinese, both genders**

In a second terminal:

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8880/tts" -Method Post -ContentType "application/json" -Body '{"text":"Hello, this is a test of the English voice."}' -OutFile "en_test.wav"
Invoke-WebRequest -Uri "http://127.0.0.1:8880/tts" -Method Post -ContentType "application/json" -Body '{"text":"你好，这是中文语音测试。","voice_gender":"male"}' -OutFile "zh_male_test.wav"
Invoke-WebRequest -Uri "http://127.0.0.1:8880/tts" -Method Post -ContentType "application/json" -Body '{"text":"你好，这是中文语音测试。","voice_gender":"female"}' -OutFile "zh_female_test.wav"
(New-Object Media.SoundPlayer "en_test.wav").PlaySync()
(New-Object Media.SoundPlayer "zh_male_test.wav").PlaySync()
(New-Object Media.SoundPlayer "zh_female_test.wav").PlaySync()
```

Expected: each `Invoke-WebRequest` returns 200 and writes a playable `.wav` file; playback sounds like the intended language (English vs. Mandarin) and the two Chinese clips sound like different voices/genders. If the request errors, check the `start.ps1` terminal for a traceback — most likely cause is a `kokoro`/`espeak-ng` install issue; fix it before continuing to later tasks.

- [ ] **Step 6: Verify empty text returns 400**

```powershell
Invoke-WebRequest -Uri "http://127.0.0.1:8880/tts" -Method Post -ContentType "application/json" -Body '{"text":"   "}' -SkipHttpErrorCheck
```

Expected: `StatusCode 400`.

- [ ] **Step 7: Clean up test files and commit**

```powershell
Remove-Item en_test.wav, zh_male_test.wav, zh_female_test.wav -ErrorAction SilentlyContinue
git add start.ps1 README.md
git commit -m "docs: add start script, README, and verify real Kokoro synthesis"
```

---

### Task 6: PowerShell TTS client helper

**Files:**
- Create: `C:\Users\awang\.claude\tts-client.ps1`

**Interfaces:**
- Consumes: `POST http://127.0.0.1:8880/tts` (Task 4/5).
- Produces: `Invoke-KokoroTts -Text <string> -Gender <'male'|'female'> -> [bool]`, used by Tasks 7–9.

- [ ] **Step 1: Write `tts-client.ps1`**

```powershell
function Invoke-KokoroTts {
    param(
        [Parameter(Mandatory)][string]$Text,
        [Parameter(Mandatory)][ValidateSet('male', 'female')][string]$Gender
    )

    $tempFile = $null
    try {
        $body = @{ text = $Text; voice_gender = $Gender } | ConvertTo-Json -Compress
        $tempFile = [System.IO.Path]::GetTempFileName() + '.wav'

        Invoke-WebRequest -Uri 'http://127.0.0.1:8880/tts' `
            -Method Post `
            -ContentType 'application/json' `
            -Body $body `
            -TimeoutSec 3 `
            -OutFile $tempFile `
            -ErrorAction Stop | Out-Null

        Add-Type -AssemblyName presentationCore
        $mp = New-Object System.Windows.Media.MediaPlayer
        $mp.Open([uri]$tempFile)
        $mp.Play()

        $waited = 0
        while (-not $mp.NaturalDuration.HasTimeSpan -and $waited -lt 3000) {
            Start-Sleep -Milliseconds 100
            $waited += 100
        }
        if ($mp.NaturalDuration.HasTimeSpan) {
            Start-Sleep -Seconds $mp.NaturalDuration.TimeSpan.TotalSeconds
        } else {
            Start-Sleep -Seconds 3
        }
        $mp.Close()

        return $true
    } catch {
        return $false
    } finally {
        if ($tempFile -and (Test-Path $tempFile)) {
            Remove-Item $tempFile -ErrorAction SilentlyContinue
        }
    }
}
```

- [ ] **Step 2: Manually verify success and failure paths**

With the Kokoro server from Task 5 still running:

```powershell
. "C:\Users\awang\.claude\tts-client.ps1"
Invoke-KokoroTts -Text "Testing the male voice pool." -Gender 'male'
Invoke-KokoroTts -Text "Testing the female voice pool." -Gender 'female'
```

Expected: both calls play audible speech and return `True`.

Stop the Kokoro server (Ctrl+C in its terminal), then:

```powershell
. "C:\Users\awang\.claude\tts-client.ps1"
Invoke-KokoroTts -Text "This should fail over." -Gender 'male'
```

Expected: no audio plays, call returns `False` within ~3 seconds (not hanging).

Restart the Kokoro server (`.\start.ps1` from Task 5, in `D:\dev\TTS\tts-server-with-kokoro`) before continuing to the next task.

- [ ] **Step 3: Commit**

```powershell
cd "C:\Users\awang\.claude"
git add tts-client.ps1
git commit -m "feat: add Invoke-KokoroTts helper for hook scripts"
```

(If `C:\Users\awang\.claude` is not itself a git repo, skip this commit step and note the file was created outside version control — verify with `git -C "C:\Users\awang\.claude" rev-parse --is-inside-work-tree` first.)

---

### Task 7: Route question/prompt hook through Kokoro (male voice)

**Files:**
- Modify: `C:\Users\awang\.claude\elicitation-speak.ps1`

**Interfaces:**
- Consumes: `Invoke-KokoroTts` (Task 6).

- [ ] **Step 1: Replace the `System.Speech` block with a Kokoro-first, OS-fallback flow**

Current content (for reference, lines 37-44 of the existing file):
```powershell
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
$synth.Rate   = 1
$synth.Volume = 100
$escaped = [System.Security.SecurityElement]::Escape($msg)
$ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><prosody pitch='+10%'>$escaped</prosody></speak>"
$synth.SpeakSsml($ssml)
```

Replace it with:
```powershell
. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $msg -Gender 'male'

if (-not $spoken) {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Male)
    $synth.Rate   = 1
    $synth.Volume = 100
    $escaped = [System.Security.SecurityElement]::Escape($msg)
    $ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><prosody pitch='+10%'>$escaped</prosody></speak>"
    $synth.SpeakSsml($ssml)
}
```

Leave every line before and after this block (mute check, `$msg` composition/markdown stripping, skill-name lookup, and the `question.mp3` playback block after it) unchanged.

- [ ] **Step 2: Manually verify with the server running**

```powershell
'{"tool_input":{"questions":[{"question":"Which approach do you want to use?"}]}}' | powershell.exe -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\awang\.claude\elicitation-speak.ps1"
```

Expected: the male Kokoro voice speaks the question, then `question.mp3` plays.

- [ ] **Step 3: Manually verify the fallback with the server stopped**

Stop the Kokoro server, run the same command again.

Expected: no error is shown; the OS voice speaks the question in a **male** voice (previously this fallback was female — confirm it changed), then `question.mp3` plays. Restart the Kokoro server afterward.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\awang\.claude"
git add elicitation-speak.ps1
git commit -m "feat: route question hook through Kokoro male voice, fix OS fallback gender"
```

(Skip if this directory isn't a git repo, per the note in Task 6 Step 3.)

---

### Task 8: Route completion hook through Kokoro (female voice)

**Files:**
- Modify: `C:\Users\awang\.claude\stop-speak.ps1`

**Interfaces:**
- Consumes: `Invoke-KokoroTts` (Task 6).

- [ ] **Step 1: Replace the `System.Speech` block**

Current content (lines 33-40 of the existing file):
```powershell
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
$synth.Rate   = 1
$synth.Volume = 100
$escaped = [System.Security.SecurityElement]::Escape($msg)
$ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><prosody pitch='+10%'>$escaped</prosody></speak>"
$synth.SpeakSsml($ssml)
```

Replace it with:
```powershell
. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $msg -Gender 'female'

if (-not $spoken) {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
    $synth.Rate   = 1
    $synth.Volume = 100
    $escaped = [System.Security.SecurityElement]::Escape($msg)
    $ssml = "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'><prosody pitch='+10%'>$escaped</prosody></speak>"
    $synth.SpeakSsml($ssml)
}
```

Leave the mute check, `$msg` composition, skill-name lookup, and `done.mp3` playback block unchanged.

- [ ] **Step 2: Manually verify with the server running**

```powershell
'{"last_assistant_message":"All tests are passing and the feature is complete."}' | powershell.exe -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\awang\.claude\stop-speak.ps1"
```

Expected: the female Kokoro voice speaks the message, then `done.mp3` plays.

- [ ] **Step 3: Manually verify the fallback with the server stopped**

Stop the Kokoro server, run the same command again.

Expected: no error shown; OS voice (female, unchanged) speaks the message, then `done.mp3` plays. Restart the Kokoro server afterward.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\awang\.claude"
git add stop-speak.ps1
git commit -m "feat: route completion hook through Kokoro female voice"
```

(Skip if this directory isn't a git repo, per the note in Task 6 Step 3.)

---

### Task 9: Route generic notification hook through Kokoro (female voice)

**Files:**
- Modify: `C:\Users\awang\.claude\speak.ps1`

**Interfaces:**
- Consumes: `Invoke-KokoroTts` (Task 6).

- [ ] **Step 1: Replace the `System.Speech` call**

Current content (full file, 12 lines):
```powershell
param(
    [string]$Message,
    [int]$Rate = 1,
    [int]$Volume = 100
)
if (Test-Path 'C:\Users\awang\.claude\tts-muted.flag') { exit }

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = $Rate
$synth.Volume = $Volume
$synth.Speak($Message)
```

Replace it with:
```powershell
param(
    [string]$Message,
    [int]$Rate = 1,
    [int]$Volume = 100
)
if (Test-Path 'C:\Users\awang\.claude\tts-muted.flag') { exit }

. "C:\Users\awang\.claude\tts-client.ps1"
$spoken = Invoke-KokoroTts -Text $Message -Gender 'female'

if (-not $spoken) {
    Add-Type -AssemblyName System.Speech
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female)
    $synth.Rate = $Rate
    $synth.Volume = $Volume
    $synth.Speak($Message)
}
```

- [ ] **Step 2: Manually verify with the server running**

```powershell
powershell.exe -NonInteractive -ExecutionPolicy Bypass -File "C:\Users\awang\.claude\speak.ps1" -Message "Notification received."
```

Expected: female Kokoro voice speaks the message.

- [ ] **Step 3: Manually verify the fallback with the server stopped**

Stop the Kokoro server, run the same command again.

Expected: no error shown; OS voice speaks the message in a female voice. Restart the Kokoro server afterward.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\awang\.claude"
git add speak.ps1
git commit -m "feat: route generic notification hook through Kokoro female voice"
```

(Skip if this directory isn't a git repo, per the note in Task 6 Step 3.)

---

### Task 10: End-to-end verification inside Claude Code

**Files:** none (verification only).

- [ ] **Step 1: Verify the question hook live**

With the Kokoro server running, trigger a real `AskUserQuestion` call in a Claude Code session (e.g. ask Claude to use `AskUserQuestion` to ask you something trivial).

Expected: you hear the male Kokoro voice ask the question, followed by the `question.mp3` chime.

- [ ] **Step 2: Verify the completion hook live**

Let a Claude Code turn finish normally (Stop hook fires).

Expected: you hear the female Kokoro voice read the summary, followed by the `done.mp3` chime.

- [ ] **Step 3: Verify silent fallback live**

Stop the Kokoro server. Repeat Steps 1 and 2.

Expected: both events still narrate (via OS voice, correct genders) and play their chimes, with no visible error in the Claude Code session. Restart the Kokoro server when done.

- [ ] **Step 4: Update the design spec's status (optional but recommended)**

No code change — this step is just confirming the plan's testing checklist (spec section "Testing plan", items 1–6) is fully satisfied. No commit needed for this task.
