"""A record of which books people ask about, kept in a small SQLite file.

What is stored: the book, when and where it was asked for (server and channel), the title
text typed inside the braces, a sentiment label for the message, a salted hash of the
asker, and the ID of their message (for /book, of the bot's reply). What is not: the
message text, user IDs, or names.
"""

import asyncio
import hashlib
import hmac
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .hardcover import Book
from .lookup import title_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,              -- Hardcover's book ID
    title TEXT NOT NULL,
    title_key TEXT NOT NULL,             -- for spotting duplicate Hardcover entries
    authors TEXT NOT NULL,
    series_name TEXT,
    series_position REAL,
    release_year INTEGER,
    pages INTEGER,
    genres TEXT NOT NULL,                -- JSON list
    rating REAL,
    ratings_count INTEGER,
    users_count INTEGER,                 -- as of last_seen_at; how filled-in the entry is
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS books_title_key ON books (title_key);

CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mentioned_at TEXT NOT NULL,          -- UTC, ISO 8601
    guild_id INTEGER NOT NULL,
    guild_name TEXT,
    channel_id INTEGER,
    channel_name TEXT,                   -- a thread is recorded as its parent channel
    category_name TEXT,
    source TEXT NOT NULL,                -- 'message' or 'command'
    query TEXT NOT NULL,                 -- the title text typed inside the braces
    book_id INTEGER REFERENCES books (id),   -- NULL when nothing was found
    wrong_book INTEGER NOT NULL DEFAULT 0,   -- 1 if the asker switched to another book
    sentiment_label TEXT,                -- NULL for /book, which has no message
    sentiment_score REAL,
    user_hash TEXT NOT NULL,
    message_id INTEGER,                  -- NULL for /book
    thread_id INTEGER,                   -- the thread, if the message was in one
    thread_name TEXT,                    -- its name then; a forum post is a thread too
    incomplete INTEGER NOT NULL DEFAULT 0,   -- 1 while the card lacks a cover or blurb
    reply_id INTEGER                     -- /book only: the bot's reply, so it can be edited
);
CREATE INDEX IF NOT EXISTS mentions_book ON mentions (book_id);
CREATE INDEX IF NOT EXISTS mentions_guild ON mentions (guild_id);
CREATE INDEX IF NOT EXISTS mentions_message ON mentions (message_id);

-- Filled in by hand: a duplicate Hardcover entry, and the one to count it as.
CREATE TABLE IF NOT EXISTS book_merges (
    book_id INTEGER PRIMARY KEY,
    merged_into INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Origin:
    """Where and by whom a lookup was made."""

    source: str  # 'message' or 'command'
    user_id: int
    guild_id: int
    guild_name: str | None
    channel_id: int | None
    channel_name: str | None
    category_name: str | None
    message_id: int | None = None
    thread_id: int | None = None
    thread_name: str | None = None


@dataclass(frozen=True)
class Miss:
    """A query in a message that found nothing when it was asked."""

    mention_id: int
    query: str
    message_id: int
    channel_id: int | None
    thread_id: int | None  # also None for a thread, in records from before this was kept


@dataclass(frozen=True)
class Incomplete:
    """A book a reply showed when Hardcover had no cover or blurb for it.

    The reply is to a message, or for /book, is found by its own ID: `reply_id`, which
    records from before it was kept don't have.
    """

    mention_id: int
    book_id: int
    message_id: int | None  # None for /book
    channel_id: int | None
    thread_id: int | None
    reply_id: int | None = None
    mentioned_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_user(salt: str, user_id: int) -> str:
    """The same person always gets the same hash, but it cannot be turned back into an ID."""
    return hmac.new(salt.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()[:32]


def connect(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    uri = f"file:{path}?mode={'ro' if read_only else 'rwc'}"
    db = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    return db


class UsageLog:
    def __init__(self, path: str, salt: str):
        self._salt = salt
        self._lock = threading.Lock()
        self._db = connect(path)
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
            # A log made before these columns existed doesn't get them from the schema above.
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(mentions)")}
            for column, kind in (
                ("thread_id", "INTEGER"),
                ("thread_name", "TEXT"),
                ("incomplete", "INTEGER NOT NULL DEFAULT 0"),
                ("reply_id", "INTEGER"),
            ):
                if column not in columns:
                    self._db.execute(f"ALTER TABLE mentions ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self._db.close()

    async def record(
        self,
        origin: Origin,
        entries: list[tuple[str, Book | None]],
        sentiment: tuple[str, float] | None,
    ) -> list[int | None]:
        """Save one mention per (query, book found or None); returns their row IDs."""
        return await asyncio.to_thread(self._record, origin, entries, sentiment)

    async def switch_book(self, mention_id: int, book: Book) -> None:
        """The asker picked a different match from "Wrong book?"."""
        await asyncio.to_thread(self._switch_book, mention_id, book)

    async def found(self, mention_id: int, book: Book) -> None:
        """A query that found nothing when it was asked finds this book now."""
        await asyncio.to_thread(self._found, mention_id, book)

    async def misses(self, hours: int) -> list[Miss]:
        """The queries in messages from the last `hours` that still have no book."""
        return await asyncio.to_thread(self._misses, hours)

    async def incomplete(self, hours: int) -> list[Incomplete]:
        """The books shown in replies from the last `hours` that lacked a cover or blurb."""
        return await asyncio.to_thread(self._incomplete, hours)

    async def replied(self, mention_ids: list[int], reply_id: int) -> None:
        """These /book lookups were answered with this message."""
        await asyncio.to_thread(self._replied, mention_ids, reply_id)

    async def completed(self, mention_ids: list[int]) -> None:
        """These replies' cards have their cover and blurb now."""
        await asyncio.to_thread(self._completed, mention_ids)

    async def forget(self, mention_ids: list[int]) -> None:
        """The asker edited these queries out of their message."""
        await asyncio.to_thread(self._forget, mention_ids)

    async def mentions_in_message(self, message_id: int) -> dict[str, int]:
        """The queries already recorded for a message, lowercased -> their row IDs."""
        return await asyncio.to_thread(self._mentions_in_message, message_id)

    def _record(self, origin, entries, sentiment) -> list[int | None]:
        label, score = sentiment or (None, None)
        user_hash = hash_user(self._salt, origin.user_id)
        ids: list[int | None] = []
        with self._lock, self._db:
            for query, book in entries:
                if book:
                    self._save_book(book)
                cursor = self._db.execute(
                    "INSERT INTO mentions (mentioned_at, guild_id, guild_name, channel_id,"
                    " channel_name, category_name, source, query, book_id, sentiment_label,"
                    " sentiment_score, user_hash, message_id, thread_id, thread_name,"
                    " incomplete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _now(),
                        origin.guild_id,
                        origin.guild_name,
                        origin.channel_id,
                        origin.channel_name,
                        origin.category_name,
                        origin.source,
                        query,
                        book.id if book else None,
                        label,
                        score,
                        user_hash,
                        origin.message_id,
                        origin.thread_id,
                        origin.thread_name,
                        int(bool(book and book.is_incomplete)),
                    ),
                )
                ids.append(cursor.lastrowid)
        return ids

    def _switch_book(self, mention_id: int, book: Book) -> None:
        with self._lock, self._db:
            self._save_book(book)
            self._db.execute(
                "UPDATE mentions SET book_id = ?, wrong_book = 1, incomplete = ? WHERE id = ?",
                (book.id, int(book.is_incomplete), mention_id),
            )

    def _found(self, mention_id: int, book: Book) -> None:
        with self._lock, self._db:
            self._save_book(book)
            self._db.execute(
                "UPDATE mentions SET book_id = ?, incomplete = ? WHERE id = ? AND book_id IS NULL",
                (book.id, int(book.is_incomplete), mention_id),
            )

    def _misses(self, hours: int) -> list[Miss]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock:
            found = self._db.execute(
                "SELECT id, query, message_id, channel_id, thread_id FROM mentions"
                " WHERE book_id IS NULL AND message_id IS NOT NULL AND mentioned_at >= ?"
                " ORDER BY id",
                (since,),
            ).fetchall()
        return [Miss(*row) for row in found]

    def _incomplete(self, hours: int) -> list[Incomplete]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._lock:
            found = self._db.execute(
                "SELECT id, book_id, message_id, channel_id, thread_id, reply_id, mentioned_at"
                " FROM mentions WHERE incomplete = 1 AND book_id IS NOT NULL"
                " AND (message_id IS NOT NULL OR source = 'command')"
                " AND mentioned_at >= ? ORDER BY id",
                (since,),
            ).fetchall()
        return [Incomplete(*row) for row in found]

    def _replied(self, mention_ids: list[int], reply_id: int) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE mentions SET reply_id = ? WHERE id = ?",
                [(reply_id, mention_id) for mention_id in mention_ids],
            )

    def _completed(self, mention_ids: list[int]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE mentions SET incomplete = 0 WHERE id = ?",
                [(mention_id,) for mention_id in mention_ids],
            )

    def _forget(self, mention_ids: list[int]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "DELETE FROM mentions WHERE id = ?", [(mention_id,) for mention_id in mention_ids]
            )

    def _mentions_in_message(self, message_id: int) -> dict[str, int]:
        with self._lock:
            found = self._db.execute(
                "SELECT id, query FROM mentions WHERE message_id = ?", (message_id,)
            ).fetchall()
        return {row["query"].casefold(): row["id"] for row in found}

    def _save_book(self, book: Book) -> None:
        now = _now()
        self._db.execute(
            "INSERT INTO books (id, title, title_key, authors, series_name, series_position,"
            " release_year, pages, genres, rating, ratings_count, users_count, first_seen_at,"
            " last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (id) DO UPDATE SET title = excluded.title,"
            " title_key = excluded.title_key, authors = excluded.authors,"
            " series_name = excluded.series_name, series_position = excluded.series_position,"
            " release_year = excluded.release_year, pages = excluded.pages,"
            " genres = excluded.genres, rating = excluded.rating,"
            " ratings_count = excluded.ratings_count, users_count = excluded.users_count,"
            " last_seen_at = excluded.last_seen_at",
            (
                book.id,
                book.title,
                title_key(book.title),
                ", ".join(book.author_names),
                book.series_name,
                book.series_position,
                book.release_year,
                book.pages,
                json.dumps(book.genres),
                book.rating,
                book.ratings_count,
                book.users_count,
                now,
                now,
            ),
        )
