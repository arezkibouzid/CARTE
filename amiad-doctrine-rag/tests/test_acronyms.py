from docrag.acronyms import expand_query, find_acronyms

TEXTS = ["The Joint Logistic Support Group (JLSG) is deployable.", "The military load classification (MLC) is a number.",
         "Heavy vehicles (in the text) are fine.", "Convoy movement (CM) rules.", "A joint logistic support group (JLSG) HQ."]


def test_acronyms_are_read_from_text_and_checked_against_initials():
    a = find_acronyms(TEXTS)
    assert a["JLSG"] == "joint logistic support group" and a["MLC"] == "military load classification"
    assert "CM" not in a or a["CM"] == "convoy movement"
    assert not any(k.lower() == "in" for k in a)


def test_long_form_gets_the_acronym_but_not_the_reverse():
    a = find_acronyms(TEXTS)
    assert "JLSG" in expand_query("What is the Joint Logistic Support Group?", a)
    assert expand_query("How is the MLC rounded?", a) == "How is the MLC rounded?"
    assert expand_query("bridge width", a) == "bridge width"