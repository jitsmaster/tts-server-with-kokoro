import random

VOICES: dict[str, dict[str, list[str]]] = {
    "en": {"male": ["am_adam", "am_michael"], "female": ["af_heart", "af_bella"]},
    "zh": {"male": ["zm_yunxi", "zm_yunjian"], "female": ["zf_xiaobei", "zf_xiaoni"]},
}


def pick_voice(language: str, gender: str) -> str:
    """Randomly select one voice id from VOICES[language][gender]."""
    return random.choice(VOICES[language][gender])
