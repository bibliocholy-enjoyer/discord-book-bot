"""What the bot's replies look like.

This is the file to edit to change the wording or layout of a reply. Nothing here talks
to Discord or Hardcover: each function takes a book that has already been looked up and
returns text (or a card) for the rest of the bot to send.

Trying out a change
-------------------
To see a sample reply in your web browser, with no accounts or keys needed:

    uv run python -m bookbot.preview

Run it again after each change. It imitates Discord closely but not exactly; to see the
real thing, restart the bot and post a {book} as usual. The sample book it shows is
defined at the top of preview.py, and you can edit that too.

Changing this file will break the tests
---------------------------------------
The tests in tests/test_formatting.py spell out the exact text of a card, letter for
letter. So as soon as you change how a card looks, some of them will fail. That is them
doing their job, not a sign you did something wrong. Run them with:

    uv run pytest tests/test_formatting.py

Each failure shows the text the code produced next to the text the test expected. If the
produced text is what you meant, open tests/test_formatting.py and change the expected
text to match. If it isn't (say it reads "None · 640 pages"), the test has caught a bug in
your change, so fix the code instead. Then run the tests again until they pass.

Discord's formatting
--------------------
Replies are written in Discord's flavor of Markdown:

    **bold**    *italic*    __underline__    ~~strikethrough~~
    [text to show](https://example.com)      a link
    ### Heading                              larger text; must start the line
    -# small print                           smaller, grayed text; must start the line
                                             (inside a card it is gray but no smaller)

A line break in the text is a line break in Discord.

What is known about a book
--------------------------
`book` below is a `Book` (defined in hardcover.py). Any of these can be shown:

    book.title            "Iron Flame"
    book.authors          a list; each has .name ("Rebecca Yarros")
    book.series_name      "The Empyrean", or None if the book is not in a series
    book.series_position  2.0, or None
    book.genres           ["Fantasy", "Romance", ...], most relevant first; may be empty
    book.description      the blurb, often several paragraphs, or None
    book.rating           average rating on Hardcover out of 5, e.g. 4.34, or None
    book.ratings_count    how many ratings that average is from
    book.release_year     2023, or None
    book.pages            640, or None
    book.cover_url        address of the cover image, or None

`links` has the three addresses for the book: links.hardcover, links.goodreads and
links.amazon. title_url(book, links) is the one the card's title links to. Almost
anything can be missing for an obscure book, so check before using it (`if book.pages:`),
or the card will say "None".
"""

import re

import discord

from .hardcover import Book
from .links import (
    Links,
    amazon_search_url,
    goodreads_search_url,
    hardcover_book_url,
    title_url,
)

# How many authors and genres to list before cutting off.
MAX_AUTHORS = 2
MAX_GENRES = 2

# The colored stripe down the left edge of each card: the pride flag's colors in order,
# with blue and violet merged into one so there is a color for each of the five books a
# reply can hold. They are softened a little to show up on both Discord's dark and light
# themes. The first card in a reply is always red, the second orange, and so on. Any
# "#rrggbb" colors work, and any number of them.
CARD_COLORS = [
    "#e5484d",  # red
    "#f7933b",  # orange
    "#f2c94c",  # yellow
    "#3fb56b",  # green
    "#737de3",  # blue-violet
]


def card_color(position: int) -> discord.Color:
    """The stripe color for the card at `position` in a reply, starting over after the last."""
    return discord.Color.from_str(CARD_COLORS[position % len(CARD_COLORS)])


# --- The book card ---------------------------------------------------------------------


def book_embed(book: Book, links: Links, position: int = 0) -> discord.Embed:
    """The card for one book. Kept short so it doesn't fill a phone screen:

        **Iron Flame** by Rebecca Yarros
        2023 · 640 pages
        *The Empyrean #2* · Fantasy, Romance
        Hardcover · Goodreads · Amazon

    or, for a book that isn't in a series, with the genres on the year's line instead:

        2023 · 316 pages · Fantasy
        Hardcover · Goodreads · Amazon

    with the cover as a small picture on the right. ("Embed" is Discord's name for a card
    like this.) `position` picks the stripe's color: 0 is the first of CARD_COLORS, 1 the
    second, and so on, starting over after the last.
    """
    # Each piece of text is built by a small function further down. A piece is an empty
    # string "" when the book doesn't have that information.
    authors = authors_text(book)
    facts = facts_text(book)
    series = series_text(book)
    genres = genres_text(book)

    # Line 1: title and author, in the card's own title field, with the title in bold.
    #
    # Discord shows that field in larger text. A heading (###) in the card's text looks
    # much the same, but on a phone it leaves a gap above itself.
    #
    # The whole field links to the book, author's name included: Discord gives the field
    # one link (the url= further down) and it can't cover just the title. That link is
    # Hardcover, or Goodreads or Amazon when Hardcover lacks the cover or blurb; see
    # title_url in links.py.
    #
    # Discord allows 256 characters here. Escaping can double a title's length, so it is
    # cut to 120 first, and the authors are left off if they wouldn't fit.
    title_line = f"**{escape(truncate(book.title, 120))}**"
    if authors and len(f"{title_line} by {authors}") <= 256:
        title_line += f" by {authors}"

    # Line 2: the year and page count, whichever are known.
    #
    # Line 3: series and genres, with a dot between them when the book has both. A book
    # that isn't in a series has its genres put on line 2 instead, saving a line; with a
    # series, all four together would be too long for a phone.
    if series:
        facts_line = facts
        details_line = " · ".join(piece for piece in [series, genres] if piece)
    else:
        facts_line = " · ".join(piece for piece in [facts, genres] if piece)
        details_line = ""

    # Line 4: where to find the book.
    links_line = (
        f"[Hardcover]({links.hardcover}) · [Goodreads]({links.goodreads})"
        f" · [Amazon]({links.amazon})"
    )

    # To add a line, build it here and put it in the list below, in the position you want
    # it. For example, to show the rating under the links:
    #
    #     rating_line = f"★ {book.rating:.2f}" if book.rating else ""
    #
    # and then: lines = [facts_line, details_line, links_line, rating_line]
    lines = [facts_line, details_line, links_line]

    # Join the lines, skipping any that came out empty so the card has no blank gaps.
    text = "\n".join(line for line in lines if line)

    embed = discord.Embed(
        title=title_line, url=title_url(book, links), description=text, color=card_color(position)
    )

    # The cover. A "thumbnail" is the small picture on the right of a card;
    # embed.set_image(url=...) would instead show it large, underneath the text.
    if book.cover_url:
        embed.set_thumbnail(url=book.cover_url)

    return embed


def fill_in_card(embed: discord.Embed, book: Book) -> None:
    """Change a card made when Hardcover lacked the cover or blurb, now that it has both.

    Of everything on the card, those two only decide the cover and where the title links
    (see title_url in links.py). Whoever fills them in often adds the year and page count
    too, so the facts line is put in or replaced as well; the rest is left as it is. A
    change to book_embed that shows the blurb, or anything else that comes with it, needs
    one here too.
    """
    embed.url = hardcover_book_url(book.id)
    embed.set_thumbnail(url=book.cover_url)
    facts = facts_text(book)
    if facts:
        lines = (embed.description or "").split("\n")
        found = _FACTS_LINE.fullmatch(lines[0])
        if found:
            # Keep any genres that follow the old year and page count.
            lines[0] = " · ".join(piece for piece in [facts, found["genres"]] if piece)
        elif lines[0] and not lines[0].startswith(("*", "[")):
            # A line of genres alone (not the series, which is in italics, or the links).
            lines[0] = f"{facts} · {lines[0]}"
        else:
            lines.insert(0, facts)
        embed.description = "\n".join(line for line in lines if line)


# What facts_text makes, to find it on a card already sent, along with any genres after
# it. Cards sent before the year lost its "Published" label still have it, so that form is
# matched too.
_FACTS_LINE = re.compile(
    r"(?:(?:Published )?\d{1,4}(?: · \d+ pages)?|\d+ pages)(?: · (?P<genres>.+))?"
)


def authors_text(book: Book) -> str:
    """The book's authors: "A, B and others"."""
    if not book.authors:
        return ""
    shown = [escape(author.name) for author in book.authors[:MAX_AUTHORS]]
    text = ", ".join(shown)
    if len(book.authors) > MAX_AUTHORS:
        text += " and others"
    return text


def series_text(book: Book) -> str:
    """The series and the book's place in it, in italics: "*The Empyrean #2*"."""
    if not book.series_name:
        return ""
    series = escape(book.series_name)
    if book.series_position is not None:
        series += f" #{format_position(book.series_position)}"
    return f"*{series}*"


def genres_text(book: Book) -> str:
    """The first few genres: "Fantasy, Romance"."""
    return ", ".join(book.genres[:MAX_GENRES])


def facts_text(book: Book) -> str:
    """The year and length, whichever are known: "2023 · 640 pages"."""
    facts = []
    if book.release_year:
        facts.append(str(book.release_year))
    if book.pages:
        facts.append(f"{book.pages} pages")
    return " · ".join(facts)


# --- Other text the bot sends ----------------------------------------------------------


def not_found_line(query: str) -> str:
    """What the bot says when it can't find a book. `query` is what the person typed."""
    shown = escape(query)
    goodreads = goodreads_search_url(query)
    amazon = amazon_search_url(query)
    # The <angle brackets> around the addresses stop Discord from adding a large preview
    # of each search page under the message.
    # "Search for it" is part of the first link, so it is clear that the links run the
    # search rather than just opening the sites.
    return (
        f"Couldn't find **{shown}** on Hardcover."
        f" [Search for it on Goodreads](<{goodreads}>) or [Amazon](<{amazon}>)."
    )


def alternative_label(book: Book) -> str:
    """One entry in the "Wrong book?" menu: "Dune — Frank Herbert (1965)".

    Menu entries are plain text: Markdown doesn't work in them, and Discord allows at
    most 100 characters.
    """
    label = book.title
    if book.authors:
        label += f" — {book.authors[0].name}"
    if book.release_year:
        label += f" ({book.release_year})"
    return truncate(label, 100)


# --- Small helpers ---------------------------------------------------------------------


def escape(text: str) -> str:
    """Put a backslash before characters that Markdown would treat as formatting (like *
    or _) so that they show up as themselves. Without this, a title such as "*batteries
    not included" would turn half the card italic.
    """
    return discord.utils.escape_markdown(text)


def truncate(text: str, limit: int) -> str:
    """Shorten text to at most `limit` characters, cutting at a space and adding "…"."""
    text = " ".join(text.split())  # also turns line breaks and runs of spaces into one space
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:—-") + "…"


def format_position(position: float) -> str:
    """A series position without a pointless ".0": 2.0 gives "2", but 2.5 stays "2.5"."""
    return str(int(position)) if float(position).is_integer() else str(position)
