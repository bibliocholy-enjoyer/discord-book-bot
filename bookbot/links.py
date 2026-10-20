"""Build links to a book on Hardcover, Goodreads and Amazon."""

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from .hardcover import Book, Edition


@dataclass(frozen=True)
class Links:
    hardcover: str
    goodreads: str
    amazon: str


HARDCOVER_BOOK = "https://hardcover.app/id/book/"
GOODREADS_BOOK = "https://www.goodreads.com/book/show/"
AMAZON_BOOK = "https://www.amazon.com/dp/"


def hardcover_book_url(book_id: int) -> str:
    return f"{HARDCOVER_BOOK}{book_id}"


def hardcover_book_id(text: str) -> int | None:
    """The book that the first hardcover_book_url in `text` links to, if there is one."""
    match = re.search(re.escape(HARDCOVER_BOOK) + r"(\d+)", text)
    return int(match.group(1)) if match else None


def goodreads_search_url(query: str) -> str:
    return f"https://www.goodreads.com/search?q={quote_plus(query[:150])}"


def amazon_search_url(query: str) -> str:
    return f"https://www.amazon.com/s?k={quote_plus(query[:150])}&i=stripbooks"


def pick_goodreads_id(editions: list[Edition], goodreads_ids: dict[int, str]) -> str | None:
    """Goodreads ID of the most popular edition that has one.

    `editions` must be ordered most popular first; `goodreads_ids` maps edition ID -> ID.
    """
    for edition in editions:
        if edition.id in goodreads_ids:
            return goodreads_ids[edition.id]
    return next(iter(goodreads_ids.values()), None)


def pick_amazon_id(editions: list[Edition]) -> str | None:
    """An ID usable in amazon.com/dp/<id>: an ASIN, or a print edition's ISBN-10.

    Amazon uses a print book's ISBN-10 as its ASIN, so either works in the URL. A
    non-audio ASIN is preferred because Hardcover's most popular edition can be a
    foreign printing whose ISBN amazon.com may not stock, and an audiobook's ASIN lands
    on the Audible listing rather than the book.
    """
    for edition in editions:
        if edition.asin and not edition.is_audio:
            return edition.asin
    for edition in editions:
        if edition.isbn_10:
            return edition.isbn_10
    for edition in editions:
        if edition.asin:
            return edition.asin
    return None


def build_links(book: Book, editions: list[Edition], goodreads_ids: dict[int, str]) -> Links:
    """Direct links where Hardcover knows the IDs, search links otherwise."""
    title_and_author = " ".join([book.title, *(a.name for a in book.authors[:1])])

    goodreads_id = pick_goodreads_id(editions, goodreads_ids)
    isbn_13 = next((i for i in book.isbns if len(i) == 13), None)
    if goodreads_id:
        goodreads = f"{GOODREADS_BOOK}{goodreads_id}"
    elif isbn_13:
        # Goodreads redirects an ISBN search straight to the book page.
        goodreads = goodreads_search_url(isbn_13)
    else:
        goodreads = goodreads_search_url(title_and_author)

    amazon_id = pick_amazon_id(editions)
    if amazon_id:
        amazon = f"{AMAZON_BOOK}{amazon_id}"
    else:
        amazon = amazon_search_url(title_and_author)

    return Links(hardcover=hardcover_book_url(book.id), goodreads=goodreads, amazon=amazon)


def title_url(book: Book, links: Links) -> str:
    """Where a card's title links: Hardcover, unless its page lacks the cover or blurb.

    Then it is the book's page on Goodreads, or else on Amazon, which nearly always have
    both. With neither, it is the Goodreads search, which is likelier to turn up the
    cover and blurb than Hardcover's bare page.
    """
    if not book.is_incomplete:
        return links.hardcover
    if links.amazon.startswith(AMAZON_BOOK) and not links.goodreads.startswith(GOODREADS_BOOK):
        return links.amazon
    return links.goodreads
