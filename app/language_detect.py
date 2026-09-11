import re

_CJK_PATTERN = re.compile(
    r"[一-鿿㐀-䶿豈-﫿]"
)
_LATIN_PATTERN = re.compile(r"[a-zA-Z]")


def detect_language(text: str) -> str:
    """Return 'zh' if CJK characters outnumber Latin letters, else 'en'."""
    cjk_count = len(_CJK_PATTERN.findall(text))
    latin_count = len(_LATIN_PATTERN.findall(text))
    return "zh" if cjk_count > latin_count else "en"
