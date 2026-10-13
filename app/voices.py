import random

VOICES: dict[str, dict[str, list[str]]] = {
    "en": {"male": ["bm_daniel"], "female": ["bf_isabella"]},
    # Kokoro-82M-v1.1-zh voices (the Mandarin model's own; the v1.0 zm_yunjian / zf_xiaobei belong to the other model)
    "zh": {"male": ["zm_010"], "female": ["zf_001"]},
}


def pick_voice(language: str, gender: str) -> str:
    """Randomly select one voice id from VOICES[language][gender]."""
    return random.choice(VOICES[language][gender])
