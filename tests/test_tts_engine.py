import threading
import time
from unittest.mock import patch

import pytest

from app import tts_engine


@pytest.fixture(autouse=True)
def _no_real_zh_assets():
    """Never download or load the real Chinese model / G2P in unit tests."""
    with patch("app.tts_engine._load_zh_model", return_value=object()), patch(
        "app.tts_engine._make_zh_g2p", return_value=object()
    ):
        yield


def _reset_pipelines():
    tts_engine._pipelines.clear()
    tts_engine._zh_voice_paths.clear()


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


def test_split_sentences_breaks_after_sentence_ends_and_semicolons_but_not_colons():
    text = "Done on: Session: Voice work; Workspace: dsh-plugins. All merged! 3.5 is fine\nNext line"
    assert tts_engine._split_sentences(text) == [
        "Done on: Session: Voice work;", "Workspace: dsh-plugins.", "All merged!", "3.5 is fine", "Next line",
    ]
    assert tts_engine._split_sentences("你好。再见！好；行") == ["你好。", "再见！", "好；", "行"]


def test_trim_silence_cuts_the_padding_the_model_leaves_around_a_phrase():
    pad = torch.zeros(2400)
    core = torch.ones(1000)
    trimmed = tts_engine._trim_silence(torch.cat([pad, core, pad]))
    keep = int(0.05 * 24000)
    assert len(trimmed) == 1000 + 2 * keep
    assert len(tts_engine._trim_silence(torch.zeros(500))) == 500  # all silence: left alone


def test_paced_sentences_are_trimmed_before_the_silence_is_added():
    class Padded(_FakePipeline):
        def __call__(self, text, voice=None, speed=1.0):
            yield None, None, torch.cat([torch.zeros(4000), torch.ones(100), torch.zeros(4000)])

    keep = int(0.05 * 24000)
    with patch("app.tts_engine._get_pipeline", return_value=Padded()):
        data = tts_engine.synthesize("One. Two.", "bm_daniel", "en", pause_s=0.5)
    assert _wav_samples(data)[0] == 2 * (100 + 2 * keep) + int(0.5 * 24000)


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


# --- Chinese runs on its own model --------------------------------------------------------

def test_chinese_pipeline_gets_the_zh_model_and_g2p_and_english_does_not():
    _reset_pipelines()
    zh_model, zh_g2p = object(), object()
    with patch("app.tts_engine._load_zh_model", return_value=zh_model), patch(
        "app.tts_engine._make_zh_g2p", return_value=zh_g2p
    ), patch("app.tts_engine.KPipeline") as mock_pipeline:
        zh = tts_engine._get_pipeline("zh")
        tts_engine._get_pipeline("en")
    zh_call, en_call = mock_pipeline.call_args_list
    assert zh_call.kwargs["lang_code"] == "z" and zh_call.kwargs["model"] is zh_model
    assert zh.g2p is zh_g2p
    assert en_call.kwargs["lang_code"] == "b" and "model" not in en_call.kwargs


def test_warm_up_survives_a_chinese_model_that_cannot_load():
    _reset_pipelines()
    with patch("app.tts_engine._load_zh_model", side_effect=OSError("offline")), patch(
        "app.tts_engine.KPipeline"
    ):
        tts_engine.warm_up()  # must not raise
    assert "en" in tts_engine._pipelines and "zh" not in tts_engine._pipelines


def test_chinese_voices_are_resolved_from_the_zh_repo_once():
    _reset_pipelines()
    with patch("app.tts_engine.hf_hub_download", return_value="C:/cache/zf_001.pt") as dl:
        assert tts_engine._zh_voice("zf_001") == "C:/cache/zf_001.pt"
        assert tts_engine._zh_voice("zf_001") == "C:/cache/zf_001.pt"
    dl.assert_called_once_with("hexgrad/Kokoro-82M-v1.1-zh", "voices/zf_001.pt")


# --- streaming ------------------------------------------------------------------------------

def test_chunk_text_splits_sentences_cuts_long_ones_and_glues_tiny_ones_forward():
    assert tts_engine.chunk_text("First sentence is long enough here. Second sentence is also long enough.") == [
        "First sentence is long enough here.", "Second sentence is also long enough.",
    ]
    assert tts_engine.chunk_text("Yes. That was a long enough second sentence.") == ["Yes. That was a long enough second sentence."]
    long_sentence = ", ".join(["a clause of some length"] * 30) + "."
    pieces = tts_engine.chunk_text(long_sentence, max_chars=100)
    assert len(pieces) > 3 and all(len(p) <= 100 for p in pieces)
    assert " ".join(pieces).replace(",", "").split() == long_sentence.replace(",", "").split()
    assert tts_engine.chunk_text("A tiny one.") == ["A tiny one."]      # nothing to glue onto: kept
    assert tts_engine.chunk_text("") == []


def test_synthesize_stream_yields_one_wav_per_phrase_with_the_gap_appended():
    fake = _FakePipeline()
    gap = int(0.25 * 24000)
    text = "The first phrase is long enough. The second phrase is long enough too."
    with patch("app.tts_engine._get_pipeline", return_value=fake):
        wavs = list(tts_engine.synthesize_stream(text, "bm_daniel", "en", speed=1.2))
    assert [c[0] for c in fake.calls] == ["The first phrase is long enough.", "The second phrase is long enough too."]
    assert all(c[1] == 1.2 for c in fake.calls)
    assert len(wavs) == 2 and all(_wav_samples(w) == (100 + gap, 24000) for w in wavs)


def test_synthesize_stream_uses_the_chinese_path_for_zh():
    with patch("app.tts_engine._get_pipeline", return_value=object()), patch(
        "app.tts_engine._synthesize_zh", return_value=[torch.ones(50)]
    ) as zh:
        wavs = list(tts_engine.synthesize_stream("今天的构建已经完成了，所有测试都通过了。", "zf_001", "zh", speed=1.1))
    assert len(wavs) == 1 and zh.call_args.args[2:] == ("zf_001", 1.1)


def test_chunk_text_counts_a_chinese_character_as_about_three_latin_ones():
    zh = "今天的构建已经完成，所有测试都通过了。我更新了三个文件，并运行了完整的测试套件。请问你想让我创建一个拉取请求吗？"
    assert tts_engine.chunk_text(zh) == [
        "今天的构建已经完成，所有测试都通过了。", "我更新了三个文件，并运行了完整的测试套件。", "请问你想让我创建一个拉取请求吗？",
    ]
    long_zh = "，".join(["这是一个相当长的分句用来测试切分"] * 20) + "。"
    pieces = tts_engine.chunk_text(long_zh)
    assert len(pieces) > 1 and all(tts_engine._weight(p) <= 240 for p in pieces)
