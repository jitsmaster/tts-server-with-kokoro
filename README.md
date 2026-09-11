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
