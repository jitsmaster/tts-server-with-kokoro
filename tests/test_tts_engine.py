import threading
import time
from unittest.mock import patch

from app import tts_engine


def _reset_pipelines():
    tts_engine._pipelines.clear()


def test_warm_up_constructs_pipeline_for_every_language():
    _reset_pipelines()
    with patch("app.tts_engine.KPipeline") as mock_pipeline:
        tts_engine.warm_up()
    assert set(tts_engine._pipelines.keys()) == set(tts_engine._LANG_CODES.keys())
    assert mock_pipeline.call_count == len(tts_engine._LANG_CODES)


def test_warm_up_is_idempotent():
    _reset_pipelines()
    with patch("app.tts_engine.KPipeline") as mock_pipeline:
        tts_engine.warm_up()
        tts_engine.warm_up()
    assert mock_pipeline.call_count == len(tts_engine._LANG_CODES)


def test_get_pipeline_is_thread_safe_for_same_language():
    _reset_pipelines()

    def slow_pipeline(*args, **kwargs):
        time.sleep(0.05)
        return object()

    with patch("app.tts_engine.KPipeline", side_effect=slow_pipeline) as mock_pipeline:
        threads = [
            threading.Thread(target=tts_engine._get_pipeline, args=("en",))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert mock_pipeline.call_count == 1


# --- pacing: speed and pauses between sentences -------------------------------------------

import io

import soundfile as sf
import torch


class _FakePipeline:
    """Stands in for KPipeline: one 100-sample chunk per call, remembers text and speed."""

    def __init__(self):
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0):
        self.calls.append((text, speed))
        yield None, None, torch.ones(100)


def _wav_samples(data):
    audio, rate = sf.read(io.BytesIO(data))
    return len(audio), rate


def test_split_sentences_breaks_after_sentence_ends_semicolons_and_colons():
    text = "Done on: Session: Voice work; Workspace: dsh-plugins. All merged! 3.5 is fine\nNext line"
    assert tts_engine._split_sentences(text) == [
        "Done on:", "Session:", "Voice work;", "Workspace:", "dsh-plugins.", "All merged!", "3.5 is fine", "Next line",
    ]
    assert tts_engine._split_sentences("你好。再见！好；行") == ["你好。", "再见！", "好；", "行"]


def test_without_pause_the_text_goes_in_whole_at_the_default_speed():
    fake = _FakePipeline()
    with patch("app.tts_engine._get_pipeline", return_value=fake):
        tts_engine.synthesize("One. Two.", "bm_daniel", "en")
    assert fake.calls == [("One. Two.", tts_engine._SPEED)]


def test_speed_is_passed_to_the_pipeline():
    fake = _FakePipeline()
    with patch("app.tts_engine._get_pipeline", return_value=fake):
        tts_engine.synthesize("One.", "bm_daniel", "en", speed=0.8)
    assert fake.calls == [("One.", 0.8)]


def test_pause_synthesizes_sentence_by_sentence_with_silence_between():
    fake = _FakePipeline()
    with patch("app.tts_engine._get_pipeline", return_value=fake):
        data = tts_engine.synthesize("One. Two. Three.", "bm_daniel", "en", speed=0.9, pause_s=0.5)
    assert fake.calls == [("One.", 0.9), ("Two.", 0.9), ("Three.", 0.9)]
    samples, rate = _wav_samples(data)
    assert rate == 24000 and samples == 3 * 100 + 2 * int(0.5 * 24000)


def test_pause_with_a_single_sentence_adds_no_silence():
    fake = _FakePipeline()
    with patch("app.tts_engine._get_pipeline", return_value=fake):
        data = tts_engine.synthesize("Only one", "bm_daniel", "en", pause_s=0.5)
    assert _wav_samples(data)[0] == 100
