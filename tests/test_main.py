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
    assert captured["voice_id"] in ["zm_yunxi", "zm_yunjian"]


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
    assert captured["voice_id"] in ["am_adam", "am_michael", "af_heart", "af_bella"]


def test_synthesis_exception_returns_500():
    def raise_error(text, voice_id, language):
        raise RuntimeError("model exploded")

    with patch("app.main.synthesize", side_effect=raise_error):
        response = client.post("/tts", json={"text": "Hello"})
    assert response.status_code == 500
