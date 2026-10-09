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


def _synthesize_zh(pipeline: KPipeline, text: str, voice_id: str, speed: float = _SPEED) -> list[torch.Tensor]:
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
            for _, _, audio in pipeline.generate_from_tokens(chunk, voice=voice_id, speed=speed):
                audio_chunks.append(audio)
    return audio_chunks


# A break after a sentence end or semicolon (ASCII ones need a space after them, so "3.5" stays whole),
# after the same marks in Chinese, or at a newline. Not at colons: "Session: Voice work" stays one phrase.
_SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?;])\s+|(?<=[。！？；])\s*|\n+)")
_SAMPLE_RATE = 24000


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_BREAK.split(text) if part and part.strip()]


def _trim_silence(audio: torch.Tensor, threshold: float = 0.01, keep_s: float = 0.05) -> torch.Tensor:
    """Cut the near-silent padding the model leaves around a phrase (keeping a little), so pauses are the ones we add."""
    loud = (audio.abs() > threshold).nonzero()
    if loud.numel() == 0:
        return audio
    keep = int(keep_s * _SAMPLE_RATE)
    return audio[max(int(loud[0]) - keep, 0) : int(loud[-1]) + 1 + keep]


def _synthesize_paced(pipeline: KPipeline, text: str, voice_id: str, language: str, speed: float, pause_s: float):
    """One synthesis per sentence with pause_s of silence between them, so each phrase is heard apart."""
    chunks: list[torch.Tensor] = []
    silence = torch.zeros(int(pause_s * _SAMPLE_RATE))
    for sentence in _split_sentences(text):
        if chunks:
            chunks.append(silence)
        if language == "zh":
            parts = _synthesize_zh(pipeline, sentence, voice_id, speed)
        else:
            parts = [audio for _, _, audio in pipeline(sentence, voice=voice_id, speed=speed)]
        if parts:
            chunks.append(_trim_silence(torch.cat(parts)))
    return chunks


def synthesize(text: str, voice_id: str, language: str, speed: float | None = None, pause_s: float = 0.0) -> bytes:
    """Run Kokoro synthesis and return WAV bytes (24kHz mono).

    speed overrides the default for this request; pause_s > 0 puts that many seconds of silence between sentences."""
    pipeline = _get_pipeline(language)
    speed = speed or _SPEED
    with _inference_lock:
        if pause_s and pause_s > 0:
            audio_chunks = _synthesize_paced(pipeline, text, voice_id, language, speed, pause_s)
        elif language == "zh":
            audio_chunks = _synthesize_zh(pipeline, text, voice_id, speed)
        else:
            audio_chunks = [audio for _, _, audio in pipeline(text, voice=voice_id, speed=speed)]
    audio = torch.cat(audio_chunks)

    buffer = io.BytesIO()
    sf.write(buffer, audio.cpu().numpy(), samplerate=_SAMPLE_RATE, format="WAV")
    return buffer.getvalue()
