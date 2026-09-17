from unittest.mock import patch

from app.voices import VOICES, pick_voice


def test_pick_voice_returns_id_from_correct_pool():
    for language in VOICES:
        for gender in VOICES[language]:
            voice_id = pick_voice(language, gender)
            assert voice_id in VOICES[language][gender]


def test_pick_voice_uses_random_choice():
    with patch("app.voices.random.choice", return_value="bm_daniel") as mock_choice:
        result = pick_voice("en", "male")
        mock_choice.assert_called_once_with(["bm_daniel"])
        assert result == "bm_daniel"
