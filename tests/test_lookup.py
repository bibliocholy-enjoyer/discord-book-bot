import pytest

from bookbot.hardcover import Author, Book, SearchHit
from bookbot.lookup import author_match, find_book, is_same_title


def hit(book_id, title, author, tokens_dropped=0, users=0):
    book = Book(
        id=book_id, title=title, authors=[Author(id=book_id, name=author)], users_count=users
    )
    return SearchHit(book=book, tokens_dropped=tokens_dropped)


class FakeClient:
    """Returns canned hits keyed by (query, titles_only) and records the calls made."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def search(self, query, *, titles_only=False, per_page=5):
        self.calls.append((query, titles_only))
        return self.responses.get((query, titles_only), [])


@pytest.mark.parametrize(
    ("wanted", "names", "expected"),
    [
        ("Alix Harrow", ["Alix E. Harrow"], 2),
        ("tolkein", ["J.R.R. Tolkien"], 1),
        ("Andy Wier", ["Andy Weir"], 2),
        ("Octavia Butler", ["Octavia E. Butler"], 2),
        ("Austen", ["Jane Austen", "Fiona Stafford"], 1),
        ("Gabriel Garcia Marquez", ["Gabriel García Márquez"], 2),
        ("Stephen King", ["Neta Jackson"], 0),
        ("", ["Jane Austen"], 0),
        ("Jane Austen", [], 0),
    ],
)
def test_author_match(wanted, names, expected):
    assert author_match(wanted, names) == expected


@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("everlasting", "The Everlasting", True),
        ("the everlasting", "Everlasting", True),
        ("Everlasting", "Everlasting", True),
        ("mistborn", "Mistborn: The Final Empire", True),
        ("forget me not garden", "Forget-Me-Not Garden", True),
        ("everlasting", "Tuck Everlasting", False),
        ("everlasting", "The Everlasting Man", False),
        ("the", "The", True),
        ("...", "The", False),
    ],
)
def test_is_same_title(query, title, expected):
    assert is_same_title(query, title) == expected


async def test_plain_query_takes_best_text_match():
    client = FakeClient(
        {("Dune", False): [hit(1, "Dune", "Frank Herbert", users=9), hit(2, "Dune 2", "X")]}
    )
    result = await find_book(client, "Dune")
    assert result.book.id == 1
    assert [b.id for b in result.alternatives] == [1, 2]
    assert client.calls == [("Dune", False), ("Dune", True)]


async def test_dropped_article_prefers_the_popular_book_with_that_title():
    client = FakeClient(
        {
            ("everlasting", False): [
                hit(1, "Everlasting", "Alyson Noël", users=91),
                hit(2, "Love Everlasting", "Tom King", users=21),
            ],
            ("everlasting", True): [
                hit(3, "The Everlasting", "Alix E. Harrow", users=971),
                hit(4, "Tuck Everlasting", "Natalie Babbitt", users=5000),
                hit(1, "Everlasting", "Alyson Noël", users=91),
            ],
        }
    )
    result = await find_book(client, "everlasting")
    assert result.book.id == 3
    assert [b.id for b in result.alternatives] == [3, 1, 2, 4]


async def test_same_title_does_not_override_a_more_popular_best_match():
    client = FakeClient(
        {
            ("hobbit", False): [
                hit(1, "The Hobbit, or There and Back Again", "J.R.R. Tolkien", users=12000)
            ],
            ("hobbit", True): [
                hit(2, "The Hobbit: An Unexpected Journey", "Jude Fisher", users=50)
            ],
        }
    )
    assert (await find_book(client, "hobbit")).book.id == 1


async def test_unhyphenated_query_matches_hyphenated_title():
    hits = [
        hit(1, "How to Survive a Summer", "Nick White", tokens_dropped=4),
        hit(2, "How to Plant a Forget-Me-Not Garden", "Ada Penrose", tokens_dropped=3),
    ]
    client = FakeClient({("how to plant a forget me not garden", False): hits})
    result = await find_book(client, "how to plant a forget me not garden")
    assert result.book.id == 2
    assert [b.id for b in result.alternatives] == [2]


async def test_by_author_picks_matching_author_not_top_hit():
    hits = [
        hit(1, "The Body", "Bill Bryson"),
        hit(2, "The Body", "Stephen King"),
        hit(3, "The Body Keeps the Score", "Bessel van der Kolk"),
    ]
    client = FakeClient({("The Body", True): hits})
    result = await find_book(client, "The Body by Stephen King")
    assert result.book.id == 2
    assert [b.id for b in result.alternatives] == [2, 1, 3]
    assert client.calls == [("The Body", True)]


async def test_full_name_match_beats_surname_only_match():
    hits = [hit(1, "Sleeping Beauties", "Owen King"), hit(2, "Sleeping Beauties", "Stephen King")]
    client = FakeClient({("Sleeping Beauties", True): hits})
    result = await find_book(client, "Sleeping Beauties by Stephen King")
    assert result.book.id == 2
    assert [b.id for b in result.alternatives] == [2, 1]


async def test_title_containing_by_falls_back_to_whole_query():
    client = FakeClient(
        {
            ("Stand", True): [hit(1, "The Stand", "Stephen King")],
            ("Stand by Me", False): [hit(2, "Stand by Me", "Neta Jackson")],
        }
    )
    result = await find_book(client, "Stand by Me")
    assert result.book.id == 2
    assert client.calls == [("Stand", True), ("Stand by Me", False), ("Stand by Me", True)]


async def test_stretched_matches_count_as_not_found():
    client = FakeClient({("zzzxq nonsense", False): [hit(1, "ZZZ", "Someone", tokens_dropped=1)]})
    result = await find_book(client, "zzzxq nonsense")
    assert result.book is None
    assert result.alternatives == []


async def test_no_hits_is_not_found():
    result = await find_book(FakeClient({}), "New Book by Jane Doe")
    assert result.book is None
