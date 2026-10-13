import re

_CJK_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]"
)


def detect_language(text: str) -> str:
    """Return 'zh' if the text contains ANY Chinese character, else 'en'.

    Mixed Chinese/English text goes to the Chinese model as a whole (it phonemizes the English words itself);
    the English model never speaks text that contains Chinese."""
    return "zh" if _CJK_PATTERN.search(text) else "en"
