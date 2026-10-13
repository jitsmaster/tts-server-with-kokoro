from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _fake_synthesize(text, voice_id, language):
    return b"RIFF....WAVEfake"


def test_empty_text_returns_400():
    response = client.post("/tts", json={"text": "   "})
    assert response.status_code == 400


def test_english_text_returns_wav_audio():
    with patch("app.main.synthesize", side_effect=_fake_synthesize):
        response = client.post("/tts", json={"text": "Hello there"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFF....WAVEfake"


def test_chinese_text_with_explicit_gender_picks_zh_pool():
    captured = {}

    def capture_synthesize(text, voice_id, language):
        captured["voice_id"] = voice_id
        captured["language"] = language
        return b"fake"

    with patch("app.main.synthesize", side_effect=capture_synthesize):
        response = client.post(
            "/tts", json={"text": "你好世界", "voice_gender": "male"}
        )
    assert response.status_code == 200
    assert captured["language"] == "zh"
    assert captured["voice_id"] == "zm_010"


def test_invalid_gender_falls_back_to_random_choice():
    captured = {}

    def capture_synthesize(text, voice_id, language):
        captured["voice_id"] = voice_id
        return b"fake"

    with patch("app.main.synthesize", side_effect=capture_synthesize):
        response = client.post(
            "/tts", json={"text": "Hello", "voice_gender": "robot"}
        )
    assert response.status_code == 200
    assert captured["voice_id"] in ["bm_daniel", "bf_isabella"]


def test_synthesis_exception_returns_500():
    def raise_error(text, voice_id, language):
        raise RuntimeError("model exploded")

    with patch("app.main.synthesize", side_effect=raise_error):
        response = client.post("/tts", json={"text": "Hello"})
    assert response.status_code == 500


def test_speed_and_pause_are_forwarded_only_when_given():
    seen = {}

    def capture(text, voice_id, language, **kwargs):
        seen.update(kwargs)
        return b"fake"

    with patch("app.main.synthesize", side_effect=capture):
        assert client.post("/tts", json={"text": "Hello. There."}).status_code == 200
        assert seen == {}
        assert client.post("/tts", json={"text": "Hello. There.", "speed": 0.85, "pause_s": 0.4}).status_code == 200
    assert seen == {"speed": 0.85, "pause_s": 0.4}


def test_speed_and_pause_outside_their_range_are_rejected():
    for body in ({"speed": 0.1}, {"speed": 3}, {"pause_s": -1}, {"pause_s": 5}):
        assert client.post("/tts", json={"text": "Hello", **body}).status_code == 422


# --- /tts/stream ----------------------------------------------------------------------------

import struct


def _read_frames(body):
    frames, pos = [], 0
    while True:
        (length,) = struct.unpack_from("<I", body, pos)
        pos += 4
        if length == 0:
            return frames, pos == len(body)
        frames.append(body[pos : pos + length])
        pos += length


def test_stream_sends_one_frame_per_phrase_and_a_zero_length_end_marker():
    def fake_stream(text, voice_id, language, **kwargs):
        yield b"WAV-ONE"
        yield b"WAV-TWO"

    with patch("app.main.synthesize_stream", side_effect=fake_stream):
        response = client.post("/tts/stream", json={"text": "One. Two."})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-wav-frames"
    frames, ended_cleanly = _read_frames(response.content)
    assert frames == [b"WAV-ONE", b"WAV-TWO"] and ended_cleanly


def test_stream_routes_language_and_forwards_speed_and_pause_as_gap():
    seen = {}

    def fake_stream(text, voice_id, language, **kwargs):
        seen.update(voice_id=voice_id, language=language, **kwargs)
        yield b"x"

    with patch("app.main.synthesize_stream", side_effect=fake_stream):
        client.post("/tts/stream", json={"text": "这是 test", "voice_gender": "female", "speed": 1.2, "pause_s": 0.3})
    assert seen == {"voice_id": "zf_001", "language": "zh", "speed": 1.2, "gap_s": 0.3}


def test_stream_empty_text_is_400_and_a_failing_first_phrase_is_a_clean_500():
    assert client.post("/tts/stream", json={"text": " "}).status_code == 400

    def boom(text, voice_id, language, **kwargs):
        raise RuntimeError("model exploded")
        yield b""

    with patch("app.main.synthesize_stream", side_effect=boom):
        assert client.post("/tts/stream", json={"text": "Hello"}).status_code == 500
