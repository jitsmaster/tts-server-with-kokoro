from app.language_detect import detect_language


def test_pure_english_is_en():
    assert detect_language("How is the weather today?") == "en"


def test_pure_chinese_is_zh():
    assert detect_language("今天天气怎么样") == "zh"


def test_majority_chinese_with_some_latin_is_zh():
    assert detect_language("这是一个测试 test") == "zh"


def test_any_chinese_at_all_routes_the_whole_text_to_the_chinese_model():
    assert detect_language("This is a long English sentence with a single 字 in it") == "zh"
    assert detect_language("Claude finished the task 完成") == "zh"


def test_no_chinese_stays_english():
    assert detect_language("This is a test of the English model, café included") == "en"


def test_no_letters_defaults_to_en():
    assert detect_language("123 !!! ???") == "en"
