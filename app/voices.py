import random

VOICES: dict[str, dict[str, list[str]]] = {
    "en": {"male": ["bm_daniel"], "female": ["bf_isabella"]},
    "zh": {"male": ["zm_yunjian"], "female": ["zf_xiaobei"]},
}


def pick_voice(language: str, gender: str) -> str:
    """Randomly select one voice id from VOICES[language][gender]."""
    return random.choice(VOICES[language][gender])
