"""Turn a free-text query into the best matching Hardcover book."""

import asyncio
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from .hardcover import Book, HardcoverClient, SearchHit
from .parsing import split_title_author

MAX_ALTERNATIVES = 5
TITLE_SIMILARITY = 0.8


@dataclass(frozen=True)
class LookupResult:
    book: Book | None
    # The chosen book first, then other plausible matches, for the "Not it?" menu.
    alternatives: list[Book] = field(default_factory=list)


def _normalize(text: str) -> str:
    """Lowercase, strip accents, and turn punctuation (including hyphens) into spaces."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _without_article(normalized: str) -> str:
    return re.sub(r"^(the|a|an) (?=\S)", "", normalized)


def title_key(title: str) -> str:
    """A title reduced for spotting duplicates: no case, accents, punctuation or article."""
    return _without_article(_normalize(title))


def is_same_title(query: str, title: str) -> bool:
    """Whether a query is a book's title, give or take a leading article or a subtitle.

    "everlasting" is "The Everlasting"; "mistborn" is "Mistborn: The Final Empire".
    """
    wanted = _without_article(_normalize(query))
    candidates = {title, title.split(":")[0]}
    return bool(wanted) and wanted in {_without_article(_normalize(c)) for c in candidates}


def title_similarity(query: str, title: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(query), _normalize(title)).ratio()


def author_match(wanted: str, author_names: list[str]) -> int:
    """2 if `wanted` is (nearly) one of the names, 1 if only the surname fits, else 0.

    Tolerates misspellings and missing initials: "Alix Harrow" ~ "Alix E. Harrow".
    """
    wanted = _normalize(wanted)
    if not wanted:
        return 0
    best = 0
    for name in author_names:
        name = _normalize(name)
        if not name:
            continue
        if difflib.SequenceMatcher(None, wanted, name).ratio() >= 0.8:
            return 2
        wanted_surname, surname = wanted.split()[-1], name.split()[-1]
        if difflib.SequenceMatcher(None, wanted_surname, surname).ratio() > 0.8:
            best = 1
    return best


def _plausible(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """Drop hits the search engine only reached by ignoring words of the query.

    Those are usually unrelated books, unless the title still reads like the query: the
    engine treats "Forget-Me-Not" as one word, so "forget me not" matches none of it.
    """
    return [
        hit
        for hit in hits
        if hit.tokens_dropped == 0 or title_similarity(query, hit.book.title) >= TITLE_SIMILARITY
    ]


def _result(pick: SearchHit, others: list[SearchHit]) -> LookupResult:
    books = [pick.book]
    for hit in others:
        if all(hit.book.id != book.id for book in books):
            books.append(hit.book)
    return LookupResult(book=books[0], alternatives=books[:MAX_ALTERNATIVES])


async def _find_by_title_and_author(
    client: HardcoverClient, title: str, author: str
) -> LookupResult | None:
    hits = _plausible(await client.search(title, titles_only=True, per_page=15), title)
    scored = [(author_match(author, hit.book.author_names), hit) for hit in hits]
    for wanted_score in (2, 1):
        by_author = [hit for score, hit in scored if score >= wanted_score]
        if by_author:
            return _result(by_author[0], by_author + hits)
    return None


async def _find_by_whole_query(client: HardcoverClient, query: str) -> LookupResult:
    # The default search ranks by text match, which buries a popular "The Everlasting"
    # under every obscure book titled exactly "Everlasting". The titles-only search ranks
    # by popularity, which surfaces it. Use both and choose between them here.
    by_match, by_popularity = await asyncio.gather(
        client.search(query, per_page=MAX_ALTERNATIVES),
        client.search(query, titles_only=True, per_page=10),
    )
    by_match, by_popularity = _plausible(by_match, query), _plausible(by_popularity, query)
    candidates = by_match + by_popularity
    if not candidates:
        return LookupResult(book=None)

    # Start from the best text match, and prefer a book whose title *is* the query only
    # if it is more popular: {hobbit} should stay "The Hobbit, or There and Back Again"
    # rather than become "The Hobbit: An Unexpected Journey - Visual Companion".
    same_title = [hit for hit in candidates if is_same_title(query, hit.book.title)]
    pick = max([candidates[0], *same_title], key=lambda hit: hit.book.users_count)
    return _result(pick, candidates)


async def find_book(client: HardcoverClient, query: str) -> LookupResult:
    """Find the book a query refers to.

    Searching "Title by Author" verbatim tends to surface study guides and box sets, so
    when the query looks like it names an author we search the title alone and pick the
    result by that author. If that finds nothing we search the whole query as written.
    """
    split = split_title_author(query)
    if split:
        result = await _find_by_title_and_author(client, *split)
        if result:
            return result
    return await _find_by_whole_query(client, query)
