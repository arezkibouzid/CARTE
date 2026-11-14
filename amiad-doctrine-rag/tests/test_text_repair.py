from docrag.text import clean, repair_words


def test_math_bold_letters_become_plain_and_hyphen_space_is_removed():
    assert clean("case -by-case \U0001d40c\U0001d40b\U0001d402") == "case-by-case MLC"


def test_cut_word_is_joined_only_when_the_whole_word_exists_elsewhere():
    out = repair_words(["rated capacit y here", "the capacity is", "capacity again", "axle x stays"])
    assert out[0] == "rated capacity here" and out[3] == "axle x stays"


def test_space_inside_brackets_is_removed():
    assert repair_words(["( e.g., this )"]) == ["(e.g., this)"]