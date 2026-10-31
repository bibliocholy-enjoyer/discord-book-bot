"""Minimal async client for the Hardcover GraphQL API (https://docs.hardcover.app/api)."""

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://api.hardcover.app/v1/graphql"
USER_AGENT = "discord-book-bot"
RATE_LIMIT_WAIT_SECONDS = 2
MAX_RATE_LIMIT_WAITS = 8

# With no fields given, Hardcover searches title, ISBNs, series, author and alternative
# titles, like the website, ranked by closeness of the text match. Restricting the search
# to titles ranks by popularity instead. Passing `fields` requires `weights` *and* `typos`
# too, otherwise the API silently returns null results.
_SEARCH = """
query Search($query: String!, $per_page: Int!) {
  search(query: $query, query_type: "Book", per_page: $per_page, page: 1) { results }
}
"""
_SEARCH_TITLES = """
query SearchTitles($query: String!, $per_page: Int!) {
  search(
    query: $query, query_type: "Book", per_page: $per_page, page: 1,
    fields: "title,alternative_titles", weights: "5,1", typos: "2,2"
  ) { results }
}
"""
# Two top-level fields, so this counts as two requests against the rate limit.
_EXTERNAL_IDS = """
query ExternalIds($book_id: Int!) {
  editions(where: {book_id: {_eq: $book_id}}, order_by: {users_count: desc}, limit: 50) {
    id
    asin
    isbn_10
    reading_format { format }
  }
  book_mappings(
    where: {book_id: {_eq: $book_id}, platform: {name: {_eq: "goodreads"}}}, limit: 50
  ) {
    edition_id
    external_id
  }
}
"""

_COVERS_AND_BLURBS = """
query CoversAndBlurbs($ids: [Int!]!) {
  books(where: {id: {_in: $ids}}) { id title description release_year pages image { url } }
}
"""


class HardcoverError(Exception):
    """The Hardcover API could not be reached or returned an error."""


@dataclass(frozen=True)
class Author:
    id: int
    name: str


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    authors: list[Author] = field(default_factory=list)
    series_name: str | None = None
    series_position: float | None = None
    description: str | None = None
    rating: float | None = None
    ratings_count: int = 0
    users_count: int = 0  # Hardcover users who have shelved the book; a popularity measure
    release_year: int | None = None
    pages: int | None = None
    genres: list[str] = field(default_factory=list)
    cover_url: str | None = None
    isbns: list[str] = field(default_factory=list)

    @property
    def author_names(self) -> list[str]:
        return [a.name for a in self.authors]

    @property
    def is_incomplete(self) -> bool:
        """Hardcover's page for the book has no cover or no blurb."""
        return not self.cover_url or not (self.description or "").strip()


@dataclass(frozen=True)
class SearchHit:
    book: Book
    # How many words of the query the search engine had to ignore to get this match.
    # Anything above zero means the match is a stretch.
    tokens_dropped: int


@dataclass(frozen=True)
class Edition:
    id: int
    asin: str | None
    isbn_10: str | None
    is_audio: bool


def book_from_search_document(doc: dict) -> Book:
    contributions = doc.get("contributions") or []
    authors = [c for c in contributions if c.get("contribution") in (None, "", "Author")]
    series = doc.get("featured_series") or {}
    image = doc.get("image") or {}
    return Book(
        id=int(doc["id"]),
        title=doc.get("title") or "Untitled",
        authors=[
            Author(id=c["author"]["id"], name=c["author"]["name"])
            for c in (authors or contributions)
            if c.get("author")
        ],
        series_name=(series.get("series") or {}).get("name"),
        series_position=series.get("position"),
        description=doc.get("description"),
        rating=doc.get("rating"),
        ratings_count=doc.get("ratings_count") or 0,
        users_count=doc.get("users_count") or 0,
        release_year=doc.get("release_year"),
        pages=doc.get("pages"),
        genres=list(doc.get("genres") or []),
        cover_url=image.get("url"),
        isbns=list(doc.get("isbns") or []),
    )


class HardcoverClient:
    def __init__(self, session: aiohttp.ClientSession, token: str):
        self._session = session
        token = token.strip()
        self._authorization = token if token.lower().startswith("bearer ") else f"Bearer {token}"

    async def search(
        self, query: str, *, titles_only: bool = False, per_page: int = 5
    ) -> list[SearchHit]:
        data = await self._graphql(
            _SEARCH_TITLES if titles_only else _SEARCH, {"query": query, "per_page": per_page}
        )
        results = (data.get("search") or {}).get("results")
        if results is None:
            raise HardcoverError(f"search returned no results object for {query!r}")
        return [
            SearchHit(
                book=book_from_search_document(hit["document"]),
                tokens_dropped=(hit.get("text_match_info") or {}).get("num_tokens_dropped", 0),
            )
            for hit in results.get("hits", [])
        ]

    async def external_ids(self, book_id: int) -> tuple[list[Edition], dict[int, str]]:
        """A book's editions (most popular first) and its Goodreads IDs keyed by edition ID."""
        data = await self._graphql(_EXTERNAL_IDS, {"book_id": book_id})
        editions = [
            Edition(
                id=e["id"],
                asin=e.get("asin") or None,
                isbn_10=e.get("isbn_10") or None,
                is_audio=(e.get("reading_format") or {}).get("format") == "Listened",
            )
            for e in data.get("editions") or []
        ]
        goodreads_ids = {
            m["edition_id"]: m["external_id"]
            for m in data.get("book_mappings") or []
            if m.get("external_id")
        }
        return editions, goodreads_ids

    async def covers_and_blurbs(self, book_ids: list[int]) -> dict[int, Book]:
        """These books as Hardcover has them now, with only the title, cover, blurb, year
        and page count.

        Search results can lag behind edits on Hardcover, so this asks for the books
        themselves.
        """
        data = await self._graphql(_COVERS_AND_BLURBS, {"ids": book_ids})
        return {
            b["id"]: Book(
                id=b["id"],
                title=b.get("title") or "Untitled",
                description=b.get("description"),
                release_year=b.get("release_year"),
                pages=b.get("pages"),
                cover_url=(b.get("image") or {}).get("url"),
            )
            for b in data.get("books") or []
        }

    async def _graphql(self, query: str, variables: dict) -> dict:
        """POST a query, retrying once on a transient failure and waiting out rate limits.

        The free tier allows a burst of 10 requests refilling at one per second, and a
        message with several {queries} can exceed that, so 429s are expected and brief.
        """
        failures = 0
        rate_limit_waits = 0
        while True:
            try:
                return await self._post(query, variables)
            except _RateLimited as error:
                rate_limit_waits += 1
                if rate_limit_waits > MAX_RATE_LIMIT_WAITS:
                    raise HardcoverError(str(error)) from error
                await asyncio.sleep(RATE_LIMIT_WAIT_SECONDS)
            except _Retryable as error:
                failures += 1
                if failures > 1:
                    raise HardcoverError(str(error)) from error
                log.warning("Hardcover request failed (%s), retrying once", error)
                await asyncio.sleep(1)

    async def _post(self, query: str, variables: dict) -> dict:
        try:
            async with self._session.post(
                API_URL,
                json={"query": query, "variables": variables},
                headers={"authorization": self._authorization, "user-agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 429:
                    raise _RateLimited((await response.text())[:300])
                if response.status >= 500 or response.status == 408:
                    raise _Retryable(f"HTTP {response.status}")
                if response.status != 200:
                    body = (await response.text())[:300]
                    raise HardcoverError(f"HTTP {response.status}: {body}")
                payload = await response.json()
        except (aiohttp.ClientError, TimeoutError) as error:
            raise _Retryable(f"{type(error).__name__}: {error}") from error
        # GraphQL reports query failures with HTTP 200 and an "errors" key.
        if payload.get("errors"):
            raise HardcoverError(f"GraphQL errors: {str(payload['errors'])[:300]}")
        return payload.get("data") or {}


class _Retryable(Exception):
    pass


class _RateLimited(Exception):
    pass
