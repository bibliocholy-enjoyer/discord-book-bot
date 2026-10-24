"""Print a summary of the usage log, or merge duplicate books.

    python -m bookbot.stats [--server NAME_OR_ID]... [--exclude NAME_OR_ID]...
                            [--all-servers] [--include-owner | --only-owner] [usage.db]
    python -m bookbot.stats merge DUPLICATE_ID REAL_ID [usage.db]

By default the report leaves out the bot's own servers (LEFT_OUT_BY_DEFAULT) and the
books the bot's owner asked for, so that it shows how other people use the bot.

--server limits the report to those servers, and then nothing is left out by default, so
`--server 'BookBot HQ'` shows that server. --exclude leaves out more servers than the
default ones, and --all-servers leaves out none by default. A server is given by its
exact name (any capitals) or its ID; the Servers section lists both.

--include-owner counts the owner like anyone else, and --only-owner shows only the owner's
books. The owner is OWNER_ID if that is set, and otherwise the owner of the bot's
application, which it asks Discord for. Telling which hash in the log is the owner's
needs USAGE_SALT; without it the owner is not left out, and the report says so.

When DISCORD_TOKEN is set, as it is on Fly, the report starts with where the bot is
installed right now, which it asks Discord for. The server filters apply to its list of
servers, but not to its count of personal accounts, which aren't in any server. Discord
only gives that count as a number, so the owner can't be taken out of it or picked out.

On Fly: fly ssh console -C "python -m bookbot.stats", or scripts/stats, which runs that
and shows the result one screen at a time. The owner can also DM the bot /stats, which
sends the same report as Discord messages.
"""

import argparse
import json
import os
import urllib.request
from dataclasses import dataclass, field

from .usage import connect, hash_user

DISCORD_API = "https://discord.com/api/v10"

# The bot's own servers, for testing it and for supporting it: what is asked for there
# says little about how people use the bot.
# By ID, so that renaming one doesn't bring it back, and no other server with the same
# name is left out; the name is only for saying what was left out.
LEFT_OUT_BY_DEFAULT = {
    "1556923641466196023": "BookBot test server",
    "1561056660674318378": "BookBot HQ",
}

# What to do with the owner's own lookups.
OWNER_CHOICES = ("leave out", "include", "only")

# Every mention counts toward the book it was merged into, if it was merged. The reports
# read this table rather than `mentions`, so they all respect the server filters.
_COUNTED = """
CREATE TEMP TABLE counted AS
SELECT m.*, COALESCE(mg.merged_into, m.book_id) AS real_book_id
FROM mentions m LEFT JOIN book_merges mg ON mg.book_id = m.book_id
"""


def server_filter(include: list[str], exclude: list[str]) -> tuple[str, list]:
    """A WHERE clause (with its values) for the servers asked for and not asked to skip."""

    def matches(servers: list[str]) -> tuple[str, list]:
        ids = [int(s) for s in servers if s.isdigit()]
        names = [s.casefold() for s in servers]
        return (
            f"(m.guild_id IN ({', '.join('?' * len(ids))})"
            f" OR LOWER(m.guild_name) IN ({', '.join('?' * len(names))}))",
            [*ids, *names],
        )

    clauses, values = [], []
    if include:
        clause, found = matches(include)
        clauses.append(clause)
        values += found
    if exclude:
        clause, found = matches(exclude)
        clauses.append(f"NOT {clause}")
        values += found
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


# Each report: a key for the bot to format it by, its title, and its query.
REPORTS = [
    (
        "overview",
        "Overview",
        "SELECT COUNT(*) AS mentions, COUNT(DISTINCT guild_id) AS servers,"
        " COUNT(DISTINCT user_hash) AS people, COUNT(DISTINCT real_book_id) AS books,"
        " SUM(book_id IS NULL) AS not_found, SUM(wrong_book) AS wrong_book_switches"
        " FROM counted",
    ),
    (
        "books",
        "Most mentioned books",
        "SELECT b.title, b.authors, COUNT(*) AS mentions,"
        " COUNT(DISTINCT c.user_hash) AS people, COUNT(DISTINCT c.guild_id) AS servers,"
        " ROUND(AVG(c.sentiment_score), 2) AS avg_sentiment"
        " FROM counted c JOIN books b ON b.id = c.real_book_id"
        # Ties go to the book asked for most recently: mentions are numbered in order.
        " GROUP BY c.real_book_id ORDER BY people DESC, mentions DESC, MAX(c.id) DESC"
        " LIMIT 10",
    ),
    (
        "servers",
        "Servers",
        "SELECT guild_name, guild_id, COUNT(*) AS mentions,"
        " SUM(source = 'message') AS braces, SUM(source = 'command') AS slash_book,"
        " COUNT(DISTINCT user_hash) AS people,"
        " MAX(mentioned_at) AS last_used FROM counted GROUP BY guild_id"
        " ORDER BY mentions DESC LIMIT 25",
    ),
    (
        # A thread, or a post in a forum channel, gets its own row under its channel.
        "channels",
        "Channels",
        "SELECT guild_name, category_name, channel_name,"
        " COALESCE(thread_name, CASE WHEN thread_id IS NOT NULL THEN '(name not known)' END)"
        " AS thread, COUNT(*) AS mentions"
        " FROM counted GROUP BY guild_id, channel_id, thread_id ORDER BY mentions DESC LIMIT 25",
    ),
    (
        "not_found",
        "Not found (most asked first)",
        "SELECT query, COUNT(*) AS times FROM counted WHERE book_id IS NULL"
        " GROUP BY LOWER(query) ORDER BY times DESC LIMIT 25",
    ),
    (
        "duplicates",
        "Possible duplicate books: same title, not yet merged (add rows to book_merges)",
        "SELECT a.title, a.id AS id, a.authors, a.users_count, b.id AS other_id,"
        " b.authors AS other_authors, b.users_count AS other_users_count"
        " FROM books a JOIN books b ON a.title_key = b.title_key AND a.id < b.id"
        " WHERE a.id NOT IN (SELECT book_id FROM book_merges)"
        " AND b.id NOT IN (SELECT book_id FROM book_merges) ORDER BY a.title LIMIT 50",
    ),
]


@dataclass
class Section:
    """One part of the report: a table, with a line about it above when there is one."""

    key: str  # what it is, for the bot to format it: "installed", or one of REPORTS
    title: str
    columns: list[str]
    rows: list[dict]
    note: str | None = None


@dataclass
class Report:
    notes: list[str]  # what was left out or picked out
    sections: list[Section] = field(default_factory=list)

    def text(self) -> str:
        """As main prints it: each section as a table."""
        lines = list(self.notes)
        for section in self.sections:
            lines.append(f"\n== {section.title} ==")
            if section.note:
                lines.append(section.note)
            rows = [tuple(row.values()) for row in section.rows]
            if rows:
                lines.append(format_table(section.columns, rows))
            elif not section.note:
                lines.append("(none yet)")
        return "\n".join(lines)


def format_table(columns: list[str], rows: list[tuple]) -> str:
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip()]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(lines)


def ask_discord(path: str, token: str):
    request = urllib.request.Request(
        DISCORD_API + path,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://github.com/bibliocholy-enjoyer/discord-book-bot, 1)",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


class NoOwner(Exception):
    """The owner's hash in the log can't be worked out."""


def owner_hash() -> str:
    """The owner's hash in the usage log, for leaving them out of the report or picking
    them out. Raises NoOwner, saying what is missing, if that can't be done."""
    salt = os.getenv("USAGE_SALT")
    if not salt:
        raise NoOwner("this needs USAGE_SALT, to work out the owner's hash")
    owner_id = os.getenv("OWNER_ID")
    if not owner_id:
        token = os.getenv("DISCORD_TOKEN")
        if not token:
            raise NoOwner("this needs OWNER_ID, or DISCORD_TOKEN to look it up")
        try:
            app = ask_discord("/applications/@me", token)
        except (OSError, ValueError) as error:
            raise NoOwner(f"could not ask Discord who the owner is: {error}") from None
        # The same choice the bot makes for its DMs: a team's owner, or else the owner.
        team = app.get("team")
        owner_id = team["owner_user_id"] if team else app["owner"]["id"]
    return hash_user(salt, int(owner_id))


def name_threads(db, token: str | None) -> None:
    """Fill in the names of threads recorded before the log kept them, by asking Discord.

    Only in the copy the reports read: the log itself is opened read-only.
    """
    columns = {row["name"] for row in db.execute("PRAGMA table_info(counted)")}
    if "thread_name" not in columns:  # a copy of the log from before the bot kept names
        db.execute("ALTER TABLE counted ADD COLUMN thread_name TEXT")
    if not token:
        return
    unnamed = db.execute(
        "SELECT thread_id FROM counted WHERE thread_id IS NOT NULL GROUP BY thread_id"
        " HAVING MAX(thread_name) IS NULL ORDER BY COUNT(*) DESC LIMIT 50"
    ).fetchall()
    for (thread_id,) in unnamed:
        try:
            name = ask_discord(f"/channels/{thread_id}", token).get("name")
        except (OSError, ValueError):
            continue  # deleted, or out of the bot's sight; the report says it isn't known
        db.execute("UPDATE counted SET thread_name = ? WHERE thread_id = ?", (name, thread_id))


def installed(
    token: str, include: list[str], exclude: list[str], owner: str = "include"
) -> Section:
    """Where the bot is installed now: its servers, and how many personal accounts.

    Discord says which servers, but for personal accounts only how many. The usage log
    can't answer this, since it only knows the servers where someone has asked for a book.

    `include` and `exclude` are the server filters, matched as server_filter does.
    `owner` is one of OWNER_CHOICES; beside the count of personal accounts, which Discord
    gives as a bare number, it says when the owner is still in it, or not alone in it.
    """

    def among(server: dict, wanted: list[str]) -> bool:
        return server["id"] in wanted or server["name"].casefold() in map(str.casefold, wanted)

    try:
        app = ask_discord("/applications/@me", token)
        # Discord sends at most 200 servers at a time, far more than the bot is in.
        servers = ask_discord("/users/@me/guilds?with_counts=true", token)
    except (OSError, ValueError) as error:
        note = f"(could not ask Discord: {error})"
        return Section("installed", "Where the bot is installed", [], [], note)
    shown = [
        s
        for s in servers
        if (not include or among(s, include)) and not (exclude and among(s, exclude))
    ]
    rows = sorted(
        (
            {"server": s["name"], "id": s["id"], "members": s.get("approximate_member_count")}
            for s in shown
        ),
        key=lambda row: -(row["members"] or 0),
    )
    count = str(len(rows)) if len(rows) == len(servers) else f"{len(rows)} of {len(servers)}"
    # Discord only gives an estimate of this number, and it can lag behind. Say so when
    # Discord leaves it out, since that is not the same as nobody having installed it.
    accounts = app.get("approximate_user_install_count")
    accounts = "unknown (Discord didn't say)" if accounts is None else f"about {accounts}"
    if owner == "leave out":
        accounts += " (the owner is not left out of this)"
    elif owner == "only":
        accounts += " (everyone, not just the owner)"
    # No table when the filters leave no servers, or the bot isn't in any.
    return Section(
        "installed",
        f"Where {app['name']} is installed",
        ["server", "id", "members"],
        rows,
        f"Servers: {count}. Personal accounts: {accounts}.",
    )


def merge(path: str, duplicate: int, real: int) -> None:
    """Count mentions of `duplicate` as mentions of `real` in every report."""
    db = connect(path)
    with db:
        known = {
            r[0] for r in db.execute("SELECT id FROM books WHERE id IN (?, ?)", (duplicate, real))
        }
        if known != {duplicate, real}:
            raise SystemExit(
                f"Both books must be in the log; missing { ({duplicate, real} - known) }"
            )
        # Anything already merged into the duplicate follows it, so merges never chain.
        db.execute(
            "UPDATE book_merges SET merged_into = ? WHERE merged_into = ?", (real, duplicate)
        )
        db.execute("INSERT OR REPLACE INTO book_merges VALUES (?, ?)", (duplicate, real))
    print(f"Book {duplicate} now counts as book {real}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize the usage log.")
    parser.add_argument("--server", action="append", default=[], metavar="NAME_OR_ID")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME_OR_ID")
    parser.add_argument(
        "--all-servers", action="store_true", help="leave out no servers by default"
    )
    owner = parser.add_mutually_exclusive_group()
    owner.add_argument("--include-owner", action="store_const", dest="owner", const="include")
    owner.add_argument("--only-owner", action="store_const", dest="owner", const="only")
    parser.add_argument("args", nargs="*", help="[merge DUPLICATE_ID REAL_ID] [path to usage.db]")
    options = parser.parse_args()
    args = options.args
    if args[:1] == ["merge"]:
        if len(args) < 3 or not (args[1].isdigit() and args[2].isdigit()):
            raise SystemExit("usage: python -m bookbot.stats merge DUPLICATE_ID REAL_ID [DB]")
        path = args[3] if len(args) > 3 else os.getenv("USAGE_DB")
        if not path:
            raise SystemExit("Give the path to usage.db, or set USAGE_DB")
        return merge(path, int(args[1]), int(args[2]))
    path = args[0] if args else os.getenv("USAGE_DB")
    if not path:
        raise SystemExit("usage: python -m bookbot.stats [PATH_TO_USAGE_DB] (or set USAGE_DB)")
    mode, note = options.owner or "leave out", None
    try:
        owner = owner_hash() if mode != "include" else None
    except NoOwner as error:
        if mode == "only":
            raise SystemExit(f"--only-owner: {error}") from None
        # Leaving the owner out is only the default, so the report goes on without it.
        mode, owner, note = "include", None, f"Not leaving out the owner: {error}"
    text = gather(
        path,
        options.server,
        options.exclude,
        all_servers=options.all_servers,
        owner=owner,
        owner_mode=mode,
        token=os.getenv("DISCORD_TOKEN"),
    ).text()
    print(f"{note}\n{text}" if note else text)


def gather(
    path: str,
    include: list[str],
    exclude: list[str],
    *,
    all_servers: bool = False,
    owner: str | None = None,
    owner_mode: str = "include",
    token: str | None = None,
) -> Report:
    """The whole report, which main prints and /stats sends.

    LEFT_OUT_BY_DEFAULT is added to `exclude` unless `all_servers`, or `include` names the
    servers wanted. `owner` is the owner's hash, and `owner_mode` (one of OWNER_CHOICES)
    what to do with their books; it needs `owner` unless it is "include". With `token`,
    Discord is asked where the bot is installed and for the names of threads.
    """
    left_out = list(exclude)  # as said at the top of the report
    if not (include or all_servers):
        exclude = exclude + [id for id in LEFT_OUT_BY_DEFAULT if id not in exclude]
        left_out = [*LEFT_OUT_BY_DEFAULT.values(), *left_out]
    if owner_mode != "include" and not owner:
        raise ValueError(f"owner_mode {owner_mode!r} needs the owner's hash")
    notes = []
    db = connect(path, read_only=True)
    try:
        where, values = server_filter(include, exclude)
        if owner_mode != "include":
            where += " AND " if where else " WHERE "
            where += "m.user_hash != ?" if owner_mode == "leave out" else "m.user_hash = ?"
            values.append(owner)
        db.execute(_COUNTED + where, values)
        if include:
            notes.append("Only servers: " + ", ".join(include))
        if left_out:
            notes.append("Leaving out servers: " + ", ".join(left_out))
        if owner_mode == "leave out":
            notes.append("Leaving out the owner")
        elif owner_mode == "only":
            notes.append("Only the owner")
        report = Report(notes)
        name_threads(db, token)
        if token:
            report.sections.append(installed(token, include, exclude, owner_mode))
        for key, title, sql in REPORTS:
            cursor = db.execute(sql)
            columns = [d[0] for d in cursor.description]
            rows = [dict(row) for row in cursor.fetchall()]
            report.sections.append(Section(key, title, columns, rows))
    finally:
        db.close()
    return report


if __name__ == "__main__":
    main()
