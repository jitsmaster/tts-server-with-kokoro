import io
import re
import threading
from collections.abc import Iterator

import soundfile as sf
import torch
from huggingface_hub import hf_hub_download
from kokoro import KModel, KPipeline

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


# Chinese runs on its own model, Kokoro-82M-v1.1-zh (trained for Mandarin, ~100 voices), never on the
# English pipeline or the English-centred v1.0 weights. Pinned kokoro 0.7.16 has no repo_id support, so the
# weights/config/voices are fetched with huggingface_hub (cached after the first run) and handed to KModel.
_ZH_REPO = "hexgrad/Kokoro-82M-v1.1-zh"
_zh_voice_paths: dict[str, str] = {}


def _load_zh_model() -> KModel:
    config = hf_hub_download(_ZH_REPO, "config.json")
    weights = hf_hub_download(_ZH_REPO, "kokoro-v1_1-zh.pth")
    return KModel(config=config, model=weights).to(_DEVICE).eval()


def _make_zh_g2p():
    """Mandarin G2P for the v1.1-zh model. Latin words inside Chinese text are phonemized (G2P only, no English
    TTS model) so they are spoken by the Chinese model instead of being dropped."""
    from misaki import en, zh

    english = en.G2P(trf=False, british=False, fallback=None, unk="")
    return zh.ZHG2P(version="1.1", en_callable=lambda text: english(text)[0])


def _zh_voice(voice_id: str) -> str:
    if voice_id not in _zh_voice_paths:
        _zh_voice_paths[voice_id] = hf_hub_download(_ZH_REPO, f"voices/{voice_id}.pt")
    return _zh_voice_paths[voice_id]


def _get_pipeline(language: str) -> KPipeline:
    if language not in _pipelines:
        with _pipeline_lock:
            if language not in _pipelines:
                if language == "zh":
                    pipeline = KPipeline(lang_code=_LANG_CODES[language], model=_load_zh_model(), device=_DEVICE)
                    pipeline.g2p = _make_zh_g2p()
                else:
                    pipeline = KPipeline(lang_code=_LANG_CODES[language], device=_DEVICE)
                _pipelines[language] = pipeline
    return _pipelines[language]


def warm_up() -> None:
    """Pre-build every language's pipeline so the first real request doesn't
    pay KPipeline construction time (~20s+) inside its HTTP timeout budget.
    A language that fails to load (e.g. the Chinese model cannot be downloaded) is logged and retried on its
    first request, so it cannot take the other languages down with it."""
    for language in _LANG_CODES:
        try:
            _get_pipeline(language)
        except Exception as exc:  # noqa: BLE001
            print(f"Kokoro TTS: could not load the '{language}' pipeline at startup: {exc!r}", flush=True)


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
            for _, _, audio in pipeline.generate_from_tokens(chunk, voice=_zh_voice(voice_id), speed=speed):
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
    return _wav_bytes(torch.cat(audio_chunks))


# --- streaming: one WAV per phrase, so playback can start after the first phrase ------------------

_STREAM_GAP_S = 0.25       # silence appended to every streamed phrase (the sentence pause)
_STREAM_MAX_CHARS = 240    # longer sentences are cut at a comma / space so the first audio is never far away
_STREAM_MIN_CHARS = 20     # shorter phrases are glued onto the next one (per-call overhead beats their audio)
_CLAUSE_BREAK = re.compile(r"(?<=[,，、;；:：])\s*")


def _wav_bytes(audio: torch.Tensor) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, audio.cpu().numpy(), samplerate=_SAMPLE_RATE, format="WAV")
    return buffer.getvalue()


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _weight(text: str) -> int:
    """Length that tracks speaking time: a Chinese character carries about three Latin ones."""
    return len(text) + 2 * len(_CJK.findall(text))


def _split_long(sentence: str, limit: int) -> list[str]:
    """Cut a sentence longer than limit at clause breaks, then at spaces, then hard."""
    if _weight(sentence) <= limit:
        return [sentence]
    pieces: list[str] = []
    current = ""
    for clause in (c for c in _CLAUSE_BREAK.split(sentence) if c):
        while _weight(clause) > limit:                  # no clause break in reach: cut at a space or hard
            cut = clause.rfind(" ", 0, max(limit // 3, 1) if _CJK.search(clause) else limit)
            cut = cut if cut > 0 else (max(limit // 3, 1) if _CJK.search(clause) else limit)
            if current:
                pieces.append(current); current = ""
            pieces.append(clause[:cut].strip()); clause = clause[cut:].strip()
        if current and _weight(current) + 1 + _weight(clause) > limit:
            pieces.append(current); current = ""
        current = f"{current} {clause}".strip() if current else clause
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def chunk_text(text: str, max_chars: int = _STREAM_MAX_CHARS, min_chars: int = _STREAM_MIN_CHARS) -> list[str]:
    """Phrases to synthesize one at a time: sentences, long ones cut, tiny ones merged forward."""
    phrases: list[str] = []
    for sentence in _split_sentences(text):
        phrases.extend(_split_long(sentence, max_chars))
    merged: list[str] = []
    carry = ""
    for phrase in phrases:
        phrase = f"{carry} {phrase}".strip() if carry else phrase
        if _weight(phrase) < min_chars:
            carry = phrase
            continue
        carry = ""
        merged.append(phrase)
    if carry:
        if merged and _weight(merged[-1]) + 1 + _weight(carry) <= max_chars:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    return merged


def synthesize_stream(
    text: str, voice_id: str, language: str, speed: float | None = None, gap_s: float = _STREAM_GAP_S
) -> Iterator[bytes]:
    """Yield one complete WAV (24 kHz mono) per phrase, each with its model padding trimmed and gap_s of silence
    appended. The inference lock is held per phrase, not per request, so other requests can interleave."""
    pipeline = _get_pipeline(language)
    speed = speed or _SPEED
    silence = torch.zeros(int(gap_s * _SAMPLE_RATE))
    for phrase in chunk_text(text):
        with _inference_lock:
            if language == "zh":
                parts = _synthesize_zh(pipeline, phrase, voice_id, speed)
            else:
                parts = [audio for _, _, audio in pipeline(phrase, voice=voice_id, speed=speed)]
        if parts:
            yield _wav_bytes(torch.cat([_trim_silence(torch.cat(parts)), silence]))
