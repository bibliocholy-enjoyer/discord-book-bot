from bookbot.hardcover import Author, Book, Edition, book_from_search_document
from bookbot.links import Links, build_links, pick_amazon_id, pick_goodreads_id, title_url

BOOK = Book(
    id=7,
    title="The Everlasting",
    authors=[Author(id=3, name="Alix E. Harrow")],
    isbns=["1250799082", "9781250799081"],
)


def edition(id, asin=None, isbn_10=None, is_audio=False):
    return Edition(id=id, asin=asin, isbn_10=isbn_10, is_audio=is_audio)


def test_goodreads_id_comes_from_most_popular_edition_that_has_one():
    editions = [edition(1), edition(2), edition(3)]
    assert pick_goodreads_id(editions, {3: "333", 2: "222"}) == "222"
    assert pick_goodreads_id(editions, {99: "999"}) == "999"
    assert pick_goodreads_id(editions, {}) is None


def test_amazon_id_prefers_non_audio_asin_then_isbn_then_audio_asin():
    audio = edition(1, asin="AUDIO", is_audio=True)
    print_only = edition(2, isbn_10="1529061172")
    ebook = edition(3, asin="EBOOK")
    assert pick_amazon_id([audio, print_only, ebook]) == "EBOOK"
    assert pick_amazon_id([audio, print_only]) == "1529061172"
    assert pick_amazon_id([audio]) == "AUDIO"
    assert pick_amazon_id([edition(4)]) is None


def test_direct_links_when_ids_are_known():
    links = build_links(BOOK, [edition(1, asin="B0DVR1JTYZ")], {1: "225653560"})
    assert links.hardcover == "https://hardcover.app/id/book/7"
    assert links.goodreads == "https://www.goodreads.com/book/show/225653560"
    assert links.amazon == "https://www.amazon.com/dp/B0DVR1JTYZ"


def test_falls_back_to_isbn_then_title_search():
    links = build_links(BOOK, [], {})
    assert links.goodreads == "https://www.goodreads.com/search?q=9781250799081"
    assert links.amazon == "https://www.amazon.com/s?k=The+Everlasting+Alix+E.+Harrow&i=stripbooks"

    no_isbn = Book(id=7, title="Legends & Lattes", authors=[Author(id=1, name="Travis Baldree")])
    links = build_links(no_isbn, [], {})
    assert links.goodreads == "https://www.goodreads.com/search?q=Legends+%26+Lattes+Travis+Baldree"


def test_book_from_search_document():
    doc = {
        "id": "312460",
        "title": "Dune",
        "contributions": [
            {"author": {"id": 1, "name": "Frank Herbert"}, "contribution": "Author"},
            {"author": {"id": 2, "name": "Some Narrator"}, "contribution": "Narrator"},
        ],
        "featured_series": {"position": 1.0, "series": {"name": "Dune"}},
        "image": {"url": "https://example.com/dune.jpg", "color": "#b7d7c1"},
        "rating": 4.3,
        "ratings_count": 5000,
        "release_year": 1965,
        "pages": 612,
        "genres": ["Science Fiction"],
        "isbns": ["9780441013593"],
    }
    book = book_from_search_document(doc)
    assert book.id == 312460
    assert book.author_names == ["Frank Herbert"]
    assert (book.series_name, book.series_position) == ("Dune", 1.0)
    assert book.cover_url == "https://example.com/dune.jpg"

    sparse = book_from_search_document({"id": "5", "title": "Sparse", "featured_series": {}})
    assert sparse.authors == [] and sparse.series_name is None and sparse.cover_url is None


COMPLETE = Book(id=7, title="Dune", description="Arrakis.", cover_url="https://c/dune.jpg")
DIRECT = Links(
    hardcover="https://hardcover.app/id/book/7",
    goodreads="https://www.goodreads.com/book/show/1",
    amazon="https://www.amazon.com/dp/B000",
)
SEARCHES = Links(
    hardcover="https://hardcover.app/id/book/7",
    goodreads="https://www.goodreads.com/search?q=Dune",
    amazon="https://www.amazon.com/s?k=Dune&i=stripbooks",
)


def test_book_is_incomplete_without_a_cover_or_a_blurb():
    assert not COMPLETE.is_incomplete
    assert Book(id=7, title="Dune", description="Arrakis.").is_incomplete
    assert Book(id=7, title="Dune", cover_url="https://c/dune.jpg").is_incomplete
    assert Book(id=7, title="Dune", description=" \n", cover_url="https://c/d.jpg").is_incomplete


def test_title_links_to_hardcover_when_it_has_cover_and_blurb():
    assert title_url(COMPLETE, DIRECT) == DIRECT.hardcover


def test_title_links_to_goodreads_then_amazon_when_hardcover_lacks_something():
    no_cover = Book(id=7, title="Dune", description="Arrakis.")
    assert title_url(no_cover, DIRECT) == DIRECT.goodreads
    amazon_only = Links(DIRECT.hardcover, SEARCHES.goodreads, DIRECT.amazon)
    assert title_url(no_cover, amazon_only) == DIRECT.amazon
    # Hardcover's page has little to show, so even a search is better.
    assert title_url(no_cover, SEARCHES) == SEARCHES.goodreads
