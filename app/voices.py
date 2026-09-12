import random

VOICES: dict[str, dict[str, list[str]]] = {
    "en": {"male": ["am_michael"], "female": ["af_bella"]},
    "zh": {"male": ["zm_yunjian"], "female": ["zf_xiaobei"]},
}


def pick_voice(language: str, gender: str) -> str:
    """Randomly select one voice id from VOICES[language][gender]."""
    return random.choice(VOICES[language][gender])
