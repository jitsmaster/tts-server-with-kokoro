#!/usr/bin/env bash
# Minimal example: call the server with curl and play the returned WAV.
#
#   ./call-tts.sh "Hello from Kokoro" male
#
# Playback uses `aplay` (Linux) if present, falling back to `afplay` (macOS).
# On Windows, use call-tts.ps1 instead.

set -euo pipefail

TEXT="${1:-Hello from Kokoro.}"
GENDER="${2:-female}"
SERVER_URL="${KOKORO_TTS_URL:-http://127.0.0.1:8880/tts}"
OUT_FILE="$(mktemp --suffix=.wav 2>/dev/null || mktemp -t kokoro).wav"

curl -sS -X POST "$SERVER_URL" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1], "voice_gender": sys.argv[2]}))' "$TEXT" "$GENDER")" \
  -o "$OUT_FILE"

if command -v aplay >/dev/null 2>&1; then
  aplay "$OUT_FILE"
elif command -v afplay >/dev/null 2>&1; then
  afplay "$OUT_FILE"
else
  echo "Saved audio to $OUT_FILE (no aplay/afplay found to auto-play it)"
fi
