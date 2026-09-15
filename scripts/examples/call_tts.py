"""Minimal example: call the server with `requests` and play the returned WAV.

    python call_tts.py "Hello from Kokoro" female

Requires: pip install requests simpleaudio
(simpleaudio is only needed for local playback; omit it and just write
`response.content` to a file if you don't need to play it back here.)
"""

import io
import sys

import requests

SERVER_URL = "http://127.0.0.1:8880/tts"


def speak(text: str, gender: str | None = None) -> bytes:
    payload = {"text": text}
    if gender in ("male", "female"):
        payload["voice_gender"] = gender

    response = requests.post(SERVER_URL, json=payload, timeout=30)
    response.raise_for_status()
    return response.content


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello from Kokoro."
    gender = sys.argv[2] if len(sys.argv) > 2 else None

    audio_bytes = speak(text, gender)

    try:
        import simpleaudio

        wave_obj = simpleaudio.WaveObject.from_wave_file(io.BytesIO(audio_bytes))
        wave_obj.play().wait_done()
    except ImportError:
        out_path = "kokoro-output.wav"
        with open(out_path, "wb") as f:
            f.write(audio_bytes)
        print(f"simpleaudio not installed -- saved audio to {out_path} instead")
