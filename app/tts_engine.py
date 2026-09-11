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
