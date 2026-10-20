import pytest

from bookbot.formatting import (
    CARD_COLORS,
    alternative_label,
    book_embed,
    format_position,
    not_found_line,
    truncate,
)
from bookbot.hardcover import Author, Book
from bookbot.links import Links

LINKS = Links(
    hardcover="https://hardcover.app/id/book/7",
    goodreads="https://www.goodreads.com/book/show/1",
    amazon="https://www.amazon.com/dp/B000",
)


def test_truncate():
    assert truncate("short", 10) == "short"
    assert truncate("one two three four", 12) == "one two…"
    assert truncate("spread\n\nover   lines", 50) == "spread over lines"


def test_format_position():
    assert format_position(1.0) == "1"
    assert format_position(2.5) == "2.5"


def test_full_embed():
    book = Book(
        id=7,
        title="Iron Flame",
        authors=[Author(id=9, name="Rebecca Yarros")],
        series_name="The Empyrean",
        series_position=2.0,
        description="word " * 200,
        rating=4.3414,
        ratings_count=1227,
        release_year=2023,
        pages=640,
        genres=["Fantasy", "Romance", "Dragons", "New Adult"],
        cover_url="https://example.com/cover.jpg",
    )
    embed = book_embed(book, LINKS)
    assert embed.title == "**Iron Flame** by Rebecca Yarros"
    assert embed.url == LINKS.hardcover
    assert embed.description.split("\n") == [
        "Published 2023 · 640 pages",
        "*The Empyrean #2* · Fantasy, Romance",
        f"[Hardcover]({LINKS.hardcover}) · [Goodreads]({LINKS.goodreads})"
        f" · [Amazon]({LINKS.amazon})",
    ]
    assert embed.thumbnail.url == "https://example.com/cover.jpg"
    assert embed.color.value == 0xE5484D
    # Kept compact for phones: everything is in the title and the three lines above.
    assert embed.fields == []
    assert embed.footer.text is None


def test_many_authors_are_summarized():
    authors = [Author(id=i, name=f"Author {i}") for i in range(5)]
    embed = book_embed(Book(id=7, title="Anthology", authors=authors), LINKS)
    assert embed.title == "**Anthology** by Author 0, Author 1 and others"


def test_sparse_embed_has_title_and_links():
    embed = book_embed(Book(id=7, title="Mystery [Draft] *2*"), LINKS)
    assert embed.title == "**Mystery [Draft] \\*2\\***"
    # Hardcover has no cover or blurb for it, so the title goes where they can be found.
    assert embed.url == LINKS.goodreads
    assert embed.description.split("\n") == [
        f"[Hardcover]({LINKS.hardcover}) · [Goodreads]({LINKS.goodreads})"
        f" · [Amazon]({LINKS.amazon})",
    ]
    assert embed.thumbnail.url is None


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        ({"release_year": 2024, "pages": 312}, "Published 2024 · 312 pages"),
        ({"release_year": 2024}, "Published 2024"),
        ({"pages": 312}, "312 pages"),
    ],
)
def test_year_and_pages_get_the_line_under_the_title(facts, expected):
    embed = book_embed(Book(id=7, title="Solo", genres=["Fantasy"], **facts), LINKS)
    assert embed.description.split("\n")[:2] == [expected, "Fantasy"]


def test_a_very_long_title_fits_the_title_field():
    authors = [Author(id=1, name="_" * 60)]
    embed = book_embed(Book(id=7, title="*" * 300, authors=authors), LINKS)
    assert len(embed.title) <= 256


def test_stripe_colors_go_in_order_and_start_over():
    book = Book(id=7, title="Solo")
    colors = [str(book_embed(book, LINKS, position).color) for position in range(6)]
    assert colors == [*CARD_COLORS, CARD_COLORS[0]]


def test_genres_follow_the_title_when_year_and_pages_are_unknown():
    embed = book_embed(Book(id=7, title="Solo", genres=["Fantasy", "Romance"]), LINKS)
    assert embed.description.split("\n")[0] == "Fantasy, Romance"


def test_not_found_line_escapes_markdown_and_links_searches():
    line = not_found_line("**Bold** & Brash")
    assert r"\*\*Bold\*\*" in line
    assert "https://www.goodreads.com/search?q=%2A%2ABold%2A%2A+%26+Brash" in line
    assert "https://www.amazon.com/s?k=%2A%2ABold%2A%2A+%26+Brash" in line
    assert "[Search for it on Goodreads](" in line and "or [Amazon](" in line


def test_alternative_label():
    book = Book(id=1, title="Dune", authors=[Author(id=1, name="Frank Herbert")], release_year=1965)
    assert alternative_label(book) == "Dune — Frank Herbert (1965)"
    assert len(alternative_label(Book(id=1, title="x" * 300))) <= 100
