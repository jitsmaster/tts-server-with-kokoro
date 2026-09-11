import io
import threading

import soundfile as sf
import torch
from kokoro import KPipeline

_LANG_CODES = {"en": "a", "zh": "z"}
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_pipelines: dict[str, KPipeline] = {}
_pipeline_lock = threading.Lock()

print(f"Kokoro TTS device: {_DEVICE}", flush=True)


def _get_pipeline(language: str) -> KPipeline:
    if language not in _pipelines:
        with _pipeline_lock:
            if language not in _pipelines:
                lang_code = _LANG_CODES[language]
                _pipelines[language] = KPipeline(lang_code=lang_code, device=_DEVICE)
    return _pipelines[language]


def warm_up() -> None:
    """Pre-build every language's pipeline so the first real request doesn't
    pay KPipeline construction time (~20s+) inside its HTTP timeout budget."""
    for language in _LANG_CODES:
        _get_pipeline(language)


def synthesize(text: str, voice_id: str, language: str) -> bytes:
    """Run Kokoro synthesis and return WAV bytes (24kHz mono)."""
    pipeline = _get_pipeline(language)
    audio_chunks = [audio for _, _, audio in pipeline(text, voice=voice_id)]
    audio = torch.cat(audio_chunks)

    buffer = io.BytesIO()
    sf.write(buffer, audio.cpu().numpy(), samplerate=24000, format="WAV")
    return buffer.getvalue()
