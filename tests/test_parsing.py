import pytest

from bookbot.parsing import MAX_QUERIES_PER_MESSAGE, extract_queries, split_title_author


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("no braces here", []),
        ("try {The Everlasting by Alix Harrow}!", ["The Everlasting by Alix Harrow"]),
        ("{Dune} and {Circe}", ["Dune", "Circe"]),
        ("{Dune} then {dune} again", ["Dune"]),
        ("{  Mistborn:   The Final Empire }", ["Mistborn: The Final Empire"]),
        ("{}", []),
        ("{a}", []),
        ("{...}", []),
        ("{line one\nline two}", []),
        ("{" + "x" * 151 + "}", []),
        ('{"title": "Dune"}', []),
        ("{'a': 1}", []),
        ("if (x) { return y; }", []),
        ("{x = 1}", []),
        ("`{Dune}` and ```\n{Circe}\n``` but {Emma}", ["Emma"]),
        ("{{Dune}}", ["Dune"]),
    ],
)
def test_extract_queries(content, expected):
    assert extract_queries(content) == expected


def test_extract_queries_is_capped():
    content = " ".join(f"{{Book {i}}}" for i in range(20))
    assert len(extract_queries(content)) == MAX_QUERIES_PER_MESSAGE


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Dune by Frank Herbert", ("Dune", "Frank Herbert")),
        ("Dune BY Frank Herbert", ("Dune", "Frank Herbert")),
        ("Stand by Me by Stephen King", ("Stand by Me", "Stephen King")),
        ("The Martian - Andy Weir", ("The Martian", "Andy Weir")),
        ("Circe, Madeline Miller", ("Circe", "Madeline Miller")),
        ("The Lion, the Witch and the Wardrobe", ("The Lion", "the Witch and the Wardrobe")),
        ("Dune", None),
        ("Standby", None),
        ("by Frank Herbert", None),
    ],
)
def test_split_title_author(query, expected):
    assert split_title_author(query) == expected
