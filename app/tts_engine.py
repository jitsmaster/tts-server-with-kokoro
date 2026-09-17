import io
import re
import threading

import soundfile as sf
import torch
from kokoro import KPipeline

_LANG_CODES = {"en": "b", "zh": "z"}
_SPEED = 1.0
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_pipelines: dict[str, KPipeline] = {}
_pipeline_lock = threading.Lock()
# FastAPI runs this synchronous route in a threadpool, so concurrent requests can
# call into the same KPipeline from different threads; serialize actual inference
# so requests queue safely server-side instead of overlapping on the GPU.
_inference_lock = threading.Lock()

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


def _synthesize_zh(pipeline: KPipeline, text: str, voice_id: str) -> list[torch.Tensor]:
    """Chinese path: KPipeline.__call__ passes misaki's ZHG2P output (a
    (phonemes, tokens) tuple) straight into KModel as if it were the phoneme
    string itself, so every request ends up with the same fixed-length
    (all-padding) input regardless of text. Work around it by unpacking the
    phoneme string ourselves and feeding it through generate_from_tokens,
    which accepts a raw phoneme string directly."""
    audio_chunks = []
    for sentence in re.split(r"([\n。！？]+)", text):
        sentence = sentence.strip()
        if not sentence or re.fullmatch(r"[\n。！？]+", sentence):
            continue
        phonemes, _ = pipeline.g2p(sentence)
        if not phonemes:
            continue
        for i in range(0, len(phonemes), 510):
            chunk = phonemes[i:i + 510]
            for _, _, audio in pipeline.generate_from_tokens(chunk, voice=voice_id, speed=_SPEED):
                audio_chunks.append(audio)
    return audio_chunks


def synthesize(text: str, voice_id: str, language: str) -> bytes:
    """Run Kokoro synthesis and return WAV bytes (24kHz mono)."""
    pipeline = _get_pipeline(language)
    with _inference_lock:
        if language == "zh":
            audio_chunks = _synthesize_zh(pipeline, text, voice_id)
        else:
            audio_chunks = [audio for _, _, audio in pipeline(text, voice=voice_id, speed=_SPEED)]
    audio = torch.cat(audio_chunks)

    buffer = io.BytesIO()
    sf.write(buffer, audio.cpu().numpy(), samplerate=24000, format="WAV")
    return buffer.getvalue()
