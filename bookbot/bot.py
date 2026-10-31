"""The Discord side: watch for {queries}, reply with book embeds."""

import asyncio
import contextlib
import datetime
import logging
import re
import time
import traceback
from dataclasses import dataclass, field, replace

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .formatting import (
    alternative_label,
    book_embed,
    card_color,
    fill_in_card,
    not_found_line,
)
from .hardcover import Book, HardcoverClient, HardcoverError
from .links import (
    Links,
    amazon_search_url,
    build_links,
    goodreads_search_url,
    hardcover_book_id,
    hardcover_book_url,
    hardcover_search_url,
)
from .lookup import LookupResult, find_book
from .parsing import MAX_QUERIES_PER_MESSAGE, extract_queries
from .sentiment import score_message
from .stats import OWNER_CHOICES, Report, Section, gather
from .usage import Incomplete, Miss, Origin, UsageLog, hash_user

log = logging.getLogger(__name__)

ERROR_REACTION = "⚠️"
# If the application has a custom emoji with this name (Developer Portal > Emojis), it is
# used for the error reaction instead. Hovering a reaction shows the emoji's name, so the
# name itself tells people what happened.
ERROR_EMOJI_NAME = "bookbot_error"
# Seconds the other matches for a reply stay in memory (5-20 KB per book), so that "Wrong
# book?" opens at once while the reply is fresh. On a reply to a message the button stays
# after that: a later click reads the message again and repeats the search.
MATCHES_REMEMBERED_FOR = 60 * 60
# Seconds the "Wrong book?" button stays on a /book reply. There is no message to read
# the query from again, and the reply can only be edited through its interaction token,
# which Discord expires after 15 minutes, so the button has to be removed before then.
COMMAND_REPLY_EXPIRES_AFTER = 14 * 60
# Added to a /book reply in a server that hasn't used {…}, once for each person there.
BOOK_TIP = (
    "-# Tip: You can also type {Book Title} or {Book Title by Author} in any message, "
    "and I’ll reply with its info."
)
# Seconds /book waits for its lookup before saying it is still working on it. Discord
# wants some answer within 3 seconds of the command, counting the time it takes to arrive.
DIRECT_ANSWER_WITHIN = 2.0
# How many answered messages to remember, so that an edit can update the reply in place
# and keep any book picked with "Wrong book?". An edit to an older message, or to one
# answered before a restart, still works: the reply is found again in the channel.
REMEMBERED_MESSAGES = 500
# How far past a message to look for the bot's reply to it. The reply normally follows
# within seconds, so it is among the first few messages after.
REPLY_SEARCH_LIMIT = 30
# How far before its usage record an old /book reply is looked for (see _fetch_command_reply).
OLD_REPLY_LEEWAY = datetime.timedelta(minutes=1)
# Discord also reports an "edit" when a link preview loads or a message is pinned. For a
# message the bot doesn't remember, the edit only counts if the text changed this recently.
RECENT_EDIT = datetime.timedelta(minutes=5)
# A usage record made before threads were noted only has the thread's parent channel. To
# find such a message again, this many of the channel's open threads are tried as well.
OLD_THREADS_TRIED = 10
RETRIES_LISTED = 15  # how many updated replies /retry-missed names in its answer
COVER_RETRY_DELAYS = (1.5, 3, 6)  # seconds to wait before each re-send of a coverless edit
OWNER_DM_INTERVAL = 3600  # at most one DM per kind of error per hour
# At most one DM a day about the same missing book, or the same book missing its cover or
# blurb, however many people ask for it in the meantime.
GAP_DM_INTERVAL = 24 * 3600
HEARTBEAT_MINUTES = 5


EXPIRED = (
    "The other matches for this reply are no longer available. "
    "Ask again with the book in {curly braces} to get a fresh one."
)


@dataclass
class Card:
    """One query from a message, and what the reply shows for it."""

    query: str
    book: Book | None  # the match shown, or None if nothing was found
    alternatives: list[Book] = field(default_factory=list)  # as in LookupResult
    embed: discord.Embed | None = None
    mention_id: int | None = None  # its usage record, if one was written


@dataclass
class Answered:
    """A message the bot has replied to, remembered so that an edit can update the reply."""

    cards: list[Card]
    message: discord.Message  # the bot's reply
    # The reply's cards as last sent. Always this same list, so that anything still
    # working on the reply (see ensure_covers) sees the cards as they are now.
    embeds: list[discord.Embed]


@dataclass
class Reply:
    """What "Wrong book?" needs to know about one of the bot's replies.

    Held in memory only, so it is gone after a while and whenever the bot restarts. For
    a reply to a message the controls then put it together again (see restore_reply);
    for a /book reply they say that the other matches are gone.
    """

    requester_id: int
    embeds: list[discord.Embed]
    # For each card that has other matches: its position and the matches, chosen one first.
    menus: list[tuple[int, list[Book]]]
    # Which match each of those cards currently shows.
    selected: dict[int, int] = field(default_factory=dict)
    # The usage record of each card, by position, so a switched book can be corrected.
    mention_ids: dict[int, int] = field(default_factory=dict)
    # The found books' cards, by position, kept up to date as books are switched.
    cards: list[Card] = field(default_factory=list)
    message: discord.Message | None = None


class DeleteButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"bookbot:delete:(?P<user_id>\d+)"
):
    """Lets whoever asked for a reply remove it, as can moderators once they confirm.

    The requester's ID lives in the button's custom_id, so this keeps working across
    bot restarts without any stored state. The button has a label as well as the emoji,
    since an emoji alone in a gray box looks like a reaction and gets clicked as one.
    """

    def __init__(self, user_id: int):
        super().__init__(
            discord.ui.Button(
                emoji="🗑️",
                label="Delete",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bookbot:delete:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        is_requester = interaction.user.id == self.user_id
        if not (is_requester or interaction.permissions.manage_messages):
            await interaction.response.send_message(
                "Only the person who asked for this can delete it.", ephemeral=True
            )
            return
        await interaction.response.defer()
        if not is_requester:
            # A moderator may not know what the button does, so ask them first.
            confirm = ConfirmDelete(interaction)
            confirm.prompt = await interaction.followup.send(
                f"<@{self.user_id}> asked for this reply. Delete it for everyone?",
                view=confirm,
                ephemeral=True,
                wait=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        interaction.client.replies.pop(interaction.message.id, None)
        await interaction.delete_original_response()


class ConfirmDelete(discord.ui.View):
    """Privately asks a moderator before they delete someone else's reply.

    The moderator's click on the delete button was deferred rather than answered, so
    that interaction can still delete the reply the button is on, but only for 15
    minutes; the prompt gives up before then. It also doesn't survive a bot restart.
    """

    def __init__(self, click: discord.Interaction):
        super().__init__(timeout=10 * 60)
        self.click = click
        self.prompt: discord.WebhookMessage | None = None

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        self.click.client.replies.pop(self.click.message.id, None)
        try:
            await self.click.delete_original_response()
        except discord.NotFound:
            pass  # someone else deleted it first
        await interaction.response.edit_message(content="Deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Left the reply alone.", view=None)

    async def on_timeout(self) -> None:
        if self.prompt is not None:
            with contextlib.suppress(discord.HTTPException):
                await self.prompt.edit(
                    content="Left the reply alone. Click Delete again to remove it.", view=None
                )


class WrongBookButton(
    discord.ui.DynamicItem[discord.ui.Button], template=r"bookbot:wrong:(?P<user_id>\d+)"
):
    """Privately offers the requester the other matches for the books in a reply."""

    def __init__(self, user_id: int):
        super().__init__(
            discord.ui.Button(
                label="Wrong book?",
                style=discord.ButtonStyle.secondary,
                custom_id=f"bookbot:wrong:{user_id}",
            )
        )
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]
    ):
        return cls(int(match["user_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: BookBot = interaction.client
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who asked for this can change it.", ephemeral=True
            )
            return
        sent = interaction.message
        reply = bot.replies.get(sent.id)
        if reply is not None:
            await interaction.response.send_message(
                _prompt(reply), view=picker_view(sent.id, reply), ephemeral=True
            )
            return
        if sent.reference is None:
            # A /book reply whose button outlived its matches, as after a restart: take
            # the button away and explain.
            await interaction.response.edit_message(view=reply_view(self.user_id))
            await interaction.followup.send(EXPIRED, ephemeral=True)
            return
        # Forgotten, so the matches have to be searched for again. Discord shows
        # "interaction failed" unless it gets an answer within 3 seconds, and that can
        # take longer. Deferring buys 15 minutes.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            reply = await bot.restore_reply(sent, self.user_id)
        except Exception as error:
            await interaction.followup.send(
                "Something went wrong finding the other matches. Try again later.",
                ephemeral=True,
            )
            await bot.report_error("the 'Wrong book?' button", error)
            return
        if reply is None:
            try:
                await sent.edit(view=reply_view(self.user_id))
            except discord.HTTPException:
                pass
            await interaction.followup.send(EXPIRED, ephemeral=True)
            return
        await interaction.followup.send(
            _prompt(reply), view=picker_view(sent.id, reply), ephemeral=True
        )


class MatchSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"bookbot:pick:(?P<message_id>\d+):(?P<embed_index>\d+)",
):
    """A menu of the matches for one card of a reply, shown privately to the requester."""

    def __init__(self, message_id: int, embed_index: int, select: discord.ui.Select):
        select.custom_id = f"bookbot:pick:{message_id}:{embed_index}"
        super().__init__(select)
        self.message_id = message_id
        self.embed_index = embed_index

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Select, match: re.Match[str]
    ):
        return cls(int(match["message_id"]), int(match["embed_index"]), item)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: BookBot = interaction.client
        # Discord shows "interaction failed" unless it gets an answer within 3 seconds,
        # and the Hardcover lookups below can take longer. Deferring buys 15 minutes.
        await interaction.response.defer()
        try:
            reply = bot.replies.get(self.message_id)
            if reply is None:
                # Forgotten since this menu was drawn: put it together again.
                sent = await interaction.channel.fetch_message(self.message_id)
                reply = await bot.restore_reply(sent, interaction.user.id)
            if reply is None:
                await interaction.edit_original_response(content=EXPIRED, view=None)
                return
            alternatives = dict(reply.menus).get(self.embed_index, [])
            book = next((b for b in alternatives if str(b.id) == self.item.values[0]), None)
            if book is None:
                # The message was edited to ask for other books after this menu was
                # drawn, or the search now gives other matches than it did then.
                await interaction.edit_original_response(
                    content="That reply has changed since. Pick again:",
                    view=picker_view(self.message_id, reply),
                )
                return
            card = reply.cards[self.embed_index]
            card.book = book
            card.embed = reply.embeds[self.embed_index] = book_embed(
                book, await bot.links_for(book), self.embed_index
            )
            reply.selected[self.embed_index] = book.id
            await bot.record_switch(reply, self.embed_index, book)
            edited = await reply.message.edit(embeds=reply.embeds)
        except discord.NotFound:
            bot.replies.pop(self.message_id, None)
            await interaction.edit_original_response(
                content="That reply has been deleted.", view=None
            )
            return
        except Exception as error:
            await bot.report_error("the 'Wrong book?' menu", error)
            return
        # In the background, so the requester's menu updates without waiting on it.
        bot.spawn(bot.ensure_covers(reply.message, reply.embeds, edited))
        await interaction.edit_original_response(view=picker_view(self.message_id, reply))


def error_key(error: Exception) -> str:
    """Identifies an error for throttling: its type plus its message, minus the specifics.

    Quoted text and long numbers are dropped so that the same failure on different
    queries or messages counts as one error, while "HTTP 401" and "HTTP 500" stay distinct.
    """
    message = re.sub(r"'[^']*'|\"[^\"]*\"|\d{5,}", "", str(error))
    return f"{type(error).__name__}: {message[:80]}"


def _command_signature(command: dict) -> str:
    """The parts of a slash command's definition that users see, for comparison.

    That includes where the command is offered (servers, a DM with the bot, other DMs),
    but not who can install it: Discord fills that in differently for each application.
    """
    option_keys = ("name", "description", "type", "required", "min_length", "max_length")
    options = [
        {key: option.get(key) for key in option_keys} for option in command.get("options") or []
    ]
    contexts = sorted(command.get("contexts") or [])
    return repr((command["name"], command["description"], options, contexts))


def _covers_missing(sent: list[discord.Embed], edited: discord.Message | None) -> bool:
    """Whether Discord's copy of a message lacks a cover that the embeds we sent have.

    Discord only reports a thumbnail's dimensions once it has fetched the image, so a
    missing width means the cover is not being shown.
    """
    if edited is None:
        return False
    return any(
        ours.thumbnail.url and not theirs.thumbnail.width
        for ours, theirs in zip(sent, edited.embeds, strict=False)
    )


def _where(guild: discord.Guild | None, channel) -> dict:
    """The server and channel details a usage record keeps.

    A thread (or a post in a forum channel, which is a thread too) is recorded as its
    parent channel, with the thread's own ID and name kept beside it.
    """
    parent = getattr(channel, "parent", None)
    thread_id = channel.id if parent else None
    thread_name = getattr(channel, "name", None) if parent else None
    channel = parent or channel
    category = getattr(channel, "category", None)
    return {
        "thread_id": thread_id,
        "thread_name": thread_name,
        "guild_id": guild.id if guild else None,
        "guild_name": guild.name if guild else None,
        "channel_id": getattr(channel, "id", None),
        "channel_name": getattr(channel, "name", None),
        "category_name": category.name if category else None,
    }


def message_origin(message: discord.Message) -> Origin | None:
    """None for direct messages, which are not recorded."""
    if message.guild is None:
        return None
    return Origin(
        source="message",
        user_id=message.author.id,
        message_id=message.id,
        **_where(message.guild, message.channel),
    )


def command_origin(interaction: discord.Interaction) -> Origin | None:
    """None for direct messages, and for a server that hasn't added the bot itself.

    There, someone is using the bot from their own account, and the server never agreed
    to anything being recorded.
    """
    if interaction.guild is None or not interaction.is_guild_integration():
        return None
    return Origin(
        source="command",
        user_id=interaction.user.id,
        **_where(interaction.guild, interaction.channel),
    )


def place_text(guild: discord.Guild | None, channel) -> str:
    """Where something was asked for, by name, for the owner: "#books in **Server**".

    Not as a channel mention, which shows as "#unknown" to someone not in that server.
    """
    if guild is None:
        return "a DM"
    name = getattr(channel, "name", None)
    shown = f"#{discord.utils.escape_markdown(name)}" if name else "a channel"
    return f"{shown} in **{discord.utils.escape_markdown(guild.name)}**"


def _just_edited(message: discord.Message) -> bool:
    """Whether the message's text changed moments ago, going by Discord's own record."""
    edited_at = message.edited_at
    return edited_at is not None and discord.utils.utcnow() - edited_at < RECENT_EDIT


def _titles(books: list[Book]) -> str:
    return ", ".join(discord.utils.escape_markdown(book.title) for book in books)


def _with_cover_and_blurb(book: Book, filled: dict[int, Book]) -> Book:
    """`book`, with the cover, blurb, year and page count from `filled` if it is there."""
    now = filled.get(book.id)
    if now is None:
        return book
    return replace(
        book,
        cover_url=now.cover_url,
        description=now.description,
        release_year=now.release_year or book.release_year,
        pages=now.pages or book.pages,
    )


def missing_text(query: str, where: str, retryable: bool) -> str:
    """The owner's DM about a query that found nothing on Hardcover."""
    shown = discord.utils.escape_markdown(query)
    return (
        f"\N{LEFT-POINTING MAGNIFYING GLASS} Couldn't find **{shown}** on Hardcover"
        f" (asked for in {where}). If it isn't there, add it: "
        f"<{hardcover_search_url(query)}>"
        + (
            "\nThen `/retry-missed` puts it in the replies that asked for it."
            if retryable
            else "\n`/retry-missed` can't update this reply: it only covers {…} messages in"
            " servers that have added the bot."
        )
    )


def gap_text(book: Book, where: str, retryable: bool) -> str:
    """The owner's DM about a book whose Hardcover page lacks its cover or blurb."""
    lacks = " and ".join(
        part
        for part, missing in (
            ("cover", not book.cover_url),
            ("blurb", not (book.description or "").strip()),
        )
        if missing
    )
    by = f" by {discord.utils.escape_markdown(book.author_names[0])}" if book.author_names else ""
    return (
        f"\N{CLOSED BOOK} **{discord.utils.escape_markdown(book.title)}**{by} has no {lacks}"
        f" on Hardcover, so its card linked to Goodreads or Amazon instead (asked for in"
        f" {where}). Fill it in: <{hardcover_book_url(book.id)}>"
        + (
            "\nThen `/retry-missed` updates the replies that showed it."
            if retryable
            else "\n`/retry-missed` can't update this reply: it only covers servers that have"
            " added the bot."
        )
    )


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


def stats_messages(report: Report) -> list[str]:
    """The stats report as Discord messages: a heading per section and a line per row,
    split into messages at section breaks (or between rows) to fit Discord's limit."""
    blocks = ["\n".join(f"-# {note}" for note in report.notes)] if report.notes else []
    for section in report.sections:
        lines = [f"**{_stats_title(section)}**"]
        if section.note:
            lines.append(section.note)
        lines += [_stats_line(section.key, row) for row in section.rows]
        if not section.rows and not section.note:
            lines.append("None yet.")
        blocks.append("\n".join(lines))
    messages: list[str] = []
    for block in blocks:
        # A section too long for one message goes in line by line.
        pieces = block.splitlines() if len(block) > 2000 else [block]
        for index, piece in enumerate(pieces):
            piece = piece[:2000]
            joiner = "\n" if index else "\n\n"
            if messages and len(messages[-1]) + len(joiner) + len(piece) <= 2000:
                messages[-1] += joiner + piece
            else:
                messages.append(piece)
    return messages


def _stats_title(section: Section) -> str:
    # The script's title for duplicates says what to do about them in the database.
    return "Possible duplicate books" if section.key == "duplicates" else section.title


def _stats_line(key: str, row: dict) -> str:
    """One row of a stats section, as a line that reads well on a phone."""

    def text(value) -> str:
        return discord.utils.escape_markdown(str(value))

    if key == "overview":
        return (
            f"{_count(row['mentions'], 'lookup', 'lookups')} by "
            f"{_count(row['people'], 'person', 'people')} in "
            f"{_count(row['servers'], 'server', 'servers')}, of "
            f"{_count(row['books'], 'book', 'books')}. "
            f"Not found: {row['not_found'] or 0}. "
            f"Switched to another book: {row['wrong_book_switches'] or 0}."
        )
    if key == "installed":
        members = "?" if row["members"] is None else row["members"]
        return f"• {text(row['server'])} ({members} members) `{row['id']}`"
    if key == "books":
        by = f" by {text(row['authors'])}" if row["authors"] else ""
        mood = "" if row["avg_sentiment"] is None else f", mood {row['avg_sentiment']:+.2f}"
        return (
            f"• **{text(row['title'])}**{by}: {_count(row['people'], 'person', 'people')}, "
            f"{_count(row['mentions'], 'mention', 'mentions')}, "
            f"{_count(row['servers'], 'server', 'servers')}{mood}"
        )
    if key == "servers":
        return (
            f"• **{text(row['guild_name'])}**: {_count(row['mentions'], 'lookup', 'lookups')}"
            f" ({row['braces']} {{…}}, {row['slash_book']} /book), "
            f"{_count(row['people'], 'person', 'people')}, last {row['last_used'][:10]} "
            f"`{row['guild_id']}`"
        )
    if key == "channels":
        place = [row["guild_name"], row["category_name"], row["channel_name"], row["thread"]]
        where = " › ".join(text(part) for part in place if part)
        return f"• {where}: {_count(row['mentions'], 'lookup', 'lookups')}"
    if key == "not_found":
        times = f" ({row['times']}×)" if row["times"] > 1 else ""
        return f"• {text(row['query'])}{times}"
    if key == "duplicates":
        return (
            f"• **{text(row['title'])}**: `{row['id']}` by {text(row['authors'])} "
            f"({row['users_count']} readers) and `{row['other_id']}` by "
            f"{text(row['other_authors'])} ({row['other_users_count']} readers)"
        )
    return "• " + ", ".join(text(value) for value in row.values())


def _server_list(text: str | None) -> list[str]:
    """The servers in a /stats option: names or IDs, separated by commas."""
    return [name.strip() for name in (text or "").split(",") if name.strip()]


def _prompt(reply: Reply) -> str:
    return "Pick the right book:" if len(reply.menus) == 1 else "Pick the right books:"


def help_text(application_id: int, support_url: str | None) -> str:
    """What /help says. Links are in <> so Discord doesn't add previews under them."""
    install = f"https://discord.com/oauth2/authorize?client_id={application_id}"
    text = (
        "Put a book in curly braces in a message and I'll reply with a card about it: "
        "`{The Everlasting}`, or add the author if I find the wrong one: "
        "`{The Everlasting by Alix E. Harrow}`. "
        f"Up to {MAX_QUERIES_PER_MESSAGE} books in one message get one combined reply. "
        "`/book` does the same lookup as a command.\n"
        "\n"
        "**On a reply**\n"
        "• **Wrong book?** shows the other matches, just to you\n"
        "• **Delete** removes the reply\n"
        "• Editing your message updates the reply, and taking out the braces removes it\n"
        "\n"
        "**Add me**\n"
        f"<{install}>\n"
        "Add me to a server so `{…}` works there, or to your own account so `/book` "
        "works anywhere, DMs included."
    )
    if support_url:
        text += f"\n\nQuestions, problems or ideas? <{support_url}>"
    return text


def reply_view(requester_id: int, *, wrong_book: bool = False) -> discord.ui.View:
    """The row of buttons under a reply."""
    view = discord.ui.View(timeout=None)
    if wrong_book:
        view.add_item(WrongBookButton(requester_id))
    view.add_item(DeleteButton(requester_id))
    return view


def picker_view(message_id: int, reply: Reply) -> discord.ui.View:
    """One menu per card that has other matches, each showing the current choice."""
    view = discord.ui.View(timeout=None)
    for embed_index, alternatives in reply.menus:
        select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=alternative_label(book),
                    value=str(book.id),
                    default=book.id == reply.selected[embed_index],
                )
                for book in alternatives
            ]
        )
        view.add_item(MatchSelect(message_id, embed_index, select))
    return view


class BookBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        # Never ping anyone: replies are for the channel, not the person who asked.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.hardcover: HardcoverClient
        self._session: aiohttp.ClientSession
        self._owner_id: int | None = config.owner_id
        self._last_owner_dm: dict[str, float] = {}
        self._last_gap_dm: dict[str, float] = {}
        # Servers known to have used {…}, which get no tip about it (see _wants_tip).
        self._braces_servers: set[int] = set()
        self.error_reaction: discord.Emoji | str = ERROR_REACTION
        self.replies: dict[int, Reply] = {}  # by the ID of the bot's reply message
        self.answered: dict[int, Answered] = {}  # by the ID of the message replied to
        # For each message being handled right now: a lock, and how many are using it.
        self._locks: dict[int, tuple[asyncio.Lock, int]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self.usage: UsageLog | None = None
        # If the log can't be opened the bot still runs, without recording; the owner is
        # told once it is connected (see setup_hook).
        self._usage_error: Exception | None = None
        if config.usage_db:
            try:
                self.usage = UsageLog(config.usage_db, config.usage_salt)
            except Exception as error:
                log.exception("Could not open the usage log; running without it")
                self._usage_error = error

    async def setup_hook(self) -> None:
        self._session = aiohttp.ClientSession()
        self.hardcover = HardcoverClient(self._session, self.config.hardcover_token)
        self.add_dynamic_items(DeleteButton, WrongBookButton, MatchSelect)

        if self._owner_id is None:
            app = await self.application_info()
            self._owner_id = app.team.owner_id if app.team else app.owner.id

        # Also for people who have added the bot to their own account, who can then use
        # it anywhere: in servers that don't have the bot, and in their DMs and group DMs.
        @self.tree.command(name="book", description="Look up a book on Hardcover")
        @app_commands.describe(query='Title, or "Title by Author"')
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def book(interaction: discord.Interaction, query: app_commands.Range[str, 2, 150]):
            await self.handle_command(interaction, query)

        @self.tree.command(name="help", description="How to use the bot")
        @app_commands.allowed_installs(guilds=True, users=True)
        @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
        async def help_(interaction: discord.Interaction):
            await self.handle_help(interaction)

        # Only offered in a DM with the bot, so that no server's list of commands has one
        # its members can't use. Anyone can see it there, but it only works for the owner.
        @self.tree.command(
            name="retry-missed",
            description="Look again for books the bot couldn't find, or found without a cover",
        )
        @app_commands.describe(
            hours="How far back to go (24 if left out)",
            dry_run="Only say what would be updated, without changing anything",
        )
        @app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
        async def retry_missed(
            interaction: discord.Interaction,
            hours: app_commands.Range[int, 1, 8760] = 24,
            dry_run: bool = False,
        ):
            await self.handle_retry(interaction, hours, dry_run)

        # DM-only and owner-only, like /retry-missed. Options can't repeat in a slash
        # command, so several servers are given separated by commas.
        @self.tree.command(name="stats", description="See how people are using the bot")
        @app_commands.describe(
            server="Only these servers (names or IDs, separated by commas)",
            exclude="Also leave out these servers (names or IDs, separated by commas)",
            all_servers="Don't leave out the bot's own servers, as it does by default",
            owner="What to do with the books you asked for (left out by default)",
        )
        @app_commands.choices(
            owner=[app_commands.Choice(name=choice, value=choice) for choice in OWNER_CHOICES]
        )
        @app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
        async def stats(
            interaction: discord.Interaction,
            server: str | None = None,
            exclude: str | None = None,
            all_servers: bool = False,
            owner: str = "leave out",
        ):
            await self.handle_stats(interaction, server, exclude, all_servers, owner)

        try:
            emojis = await self.fetch_application_emojis()
            self.error_reaction = next(
                (e for e in emojis if e.name == ERROR_EMOJI_NAME), ERROR_REACTION
            )
        except discord.HTTPException:
            log.warning("Could not fetch application emojis; using %s", ERROR_REACTION)

        await self.report_usage_log_failure()

        self.tree.on_error = self._on_command_error
        self._sync_task = asyncio.create_task(self._sync_commands())
        if self.config.heartbeat_url:
            self.heartbeat.start()

    async def _sync_commands(self) -> None:
        """Register the slash commands with Discord if they aren't already as defined here.

        Runs in the background and only when something changed: Discord rate-limits
        command registration, and waiting that out during startup would leave the bot
        deaf to messages after a few quick restarts.
        """
        try:
            local = [_command_signature(c.to_dict(self.tree)) for c in self.tree.get_commands()]
            remote = [_command_signature(c.to_dict()) for c in await self.tree.fetch_commands()]
            if sorted(local) == sorted(remote):
                log.info("Slash commands are up to date")
                return
            await self.tree.sync()
            log.info("Registered slash commands with Discord")
        except Exception:
            log.exception("Could not register slash commands")

    async def close(self) -> None:
        self.heartbeat.cancel()
        await super().close()
        await self._session.close()
        if self.usage:
            self.usage.close()

    async def on_ready(self) -> None:
        log.info("Connected as %s in %d server(s)", self.user, len(self.guilds))

    # --- Looking things up -------------------------------------------------------------

    async def links_for(self, book: Book) -> Links:
        """Direct Goodreads/Amazon links when possible, search links if that lookup fails."""
        try:
            editions, goodreads_ids = await self.hardcover.external_ids(book.id)
        except HardcoverError as error:
            log.warning("Falling back to search links for book %s: %s", book.id, error)
            name = " ".join([book.title, *book.author_names[:1]])
            return Links(
                hardcover=hardcover_book_url(book.id),
                goodreads=goodreads_search_url(name),
                amazon=amazon_search_url(name),
            )
        return build_links(book, editions, goodreads_ids)

    async def _respond(
        self,
        queries: list[str],
        requester_id: int,
        origin: Origin | None = None,
        content: str | None = None,
        where: str | None = None,
    ) -> tuple[dict, Reply | None]:
        """Look up every query; return the reply's keyword arguments and what to remember.

        With an `origin`, the lookups are also written to the usage log. `content` is the
        message they came from, used only to score its sentiment and never stored. `where`
        is as in _look_up.
        """
        cards = await self._look_up(queries, origin, content, where=where)
        return await self._compose(cards, requester_id)

    async def _look_up(
        self,
        queries: list[str],
        origin: Origin | None = None,
        content: str | None = None,
        known: dict[str, Card] | None = None,
        logged: dict[str, int] | None = None,
        where: str | None = None,
    ) -> list[Card]:
        """A card for every query, in order, without its embed yet.

        When a message has been edited, `known` holds the cards its reply already has, by
        lowercased query. Those are used as they are, which keeps a book picked with
        "Wrong book?"; only the other queries are looked up and written to the usage log.
        `logged` holds the usage records of queries that are in the log already but have
        no card, as after a restart: those are looked up again but not logged twice.

        With `where` (what the owner is told the lookup was for), the owner is also told
        about each new query that found nothing, or found a book Hardcover has no cover
        or blurb for, since those are fixed by adding to Hardcover.
        """
        known, logged = known or {}, logged or {}
        cards: list[Card] = []
        new: list[Card] = []
        for query in queries:
            key = query.casefold()
            card = known.get(key)
            if card is None:
                result = await find_book(self.hardcover, query)
                card = Card(query, result.book, result.alternatives, mention_id=logged.get(key))
                if card.mention_id is None:
                    new.append(card)
            cards.append(card)
        await self.record_usage(origin, new, content)
        if where is not None:
            # /retry-missed can only update a reply it has a usage record for. It can't
            # put a found book into a /book reply, which would need the asker's ID for its
            # buttons, but it can fill in a card's cover.
            covers = bool(self.usage and origin)
            books = covers and origin.source == "message"
            self.spawn(self.report_gaps(new, where, books, covers))
        return cards

    async def _compose(
        self, cards: list[Card], requester_id: int, embeds: list[discord.Embed] | None = None
    ) -> tuple[dict, Reply | None]:
        """Turn cards into a reply's keyword arguments and what to remember for its menu.

        `embeds` is the list to fill, when the reply already exists and has one.
        """
        found = [card for card in cards if card.book]
        for index, card in enumerate(found):
            if card.embed is None:
                card.embed = book_embed(card.book, await self.links_for(card.book), index)
            else:
                card.embed.color = card_color(index)  # it may have moved up or down
        embeds = [] if embeds is None else embeds
        embeds[:] = [card.embed for card in found]
        missing = [not_found_line(card.query) for card in cards if not card.book]

        menus = [
            (index, card.alternatives)
            for index, card in enumerate(found)
            if len(card.alternatives) > 1
        ]
        kwargs: dict = {"embeds": embeds}
        if missing:
            kwargs["content"] = "\n".join(missing)
        if embeds:
            kwargs["view"] = reply_view(requester_id, wrong_book=bool(menus))
        if not menus:
            return kwargs, None
        selected = {index: found[index].book.id for index, _ in menus}
        mention_ids = {
            index: card.mention_id for index, card in enumerate(found) if card.mention_id
        }
        return kwargs, Reply(requester_id, embeds, menus, selected, mention_ids, found)

    # --- Usage log ---------------------------------------------------------------------

    async def record_usage(
        self, origin: Origin | None, cards: list[Card], content: str | None
    ) -> None:
        """Log the lookups behind `cards`, noting on each card which record is its own.

        Never raises: a broken log must not stop anyone getting their book.
        """
        if not (self.usage and origin and cards):
            return
        try:
            sentiment = score_message(content) if content else None
            ids = await self.usage.record(origin, [(c.query, c.book) for c in cards], sentiment)
            for card, mention_id in zip(cards, ids, strict=True):
                card.mention_id = mention_id
        except Exception as error:
            await self.report_error("recording usage", error)

    async def record_reply(self, mention_ids: list[int], reply_id: int) -> None:
        """Note which message answered these /book lookups. Never raises, as above."""
        try:
            await self.usage.replied(mention_ids, reply_id)
        except Exception as error:
            await self.report_error("recording a /book reply", error)

    async def forget_usage(self, mention_ids: list[int]) -> None:
        """Remove the records of queries that were edited out of a message."""
        if not (self.usage and mention_ids):
            return
        try:
            await self.usage.forget(mention_ids)
        except Exception as error:
            await self.report_error("removing an edited-out query from the usage log", error)

    async def logged_mentions(self, message: discord.Message) -> dict[str, int]:
        """The queries the usage log already has for a message -> their records."""
        if not (self.usage and message.guild):
            return {}
        try:
            return await self.usage.mentions_in_message(message.id)
        except Exception as error:
            await self.report_error("reading the usage log", error)
            return {}

    async def record_found(self, card: Card) -> None:
        """Correct the record of a query that found nothing at first."""
        if not (self.usage and card.mention_id):
            return
        try:
            await self.usage.found(card.mention_id, card.book)
        except Exception as error:
            await self.report_error("recording a book found on a later try", error)

    async def report_usage_log_failure(self) -> None:
        """Tell the owner the log couldn't be opened, if that happened at startup."""
        if self._usage_error:
            await self.report_error(
                "opening the usage log (nothing is being recorded)", self._usage_error
            )

    async def record_switch(self, reply: Reply, embed_index: int, book: Book) -> None:
        mention_id = reply.mention_ids.get(embed_index)
        if not (self.usage and mention_id):
            return
        try:
            await self.usage.switch_book(mention_id, book)
        except Exception as error:
            await self.report_error("recording a changed book", error)

    def _sent(
        self,
        reply: Reply | None,
        kwargs: dict,
        message: discord.Message,
        forget_after: float,
        retire: bool = False,
    ) -> None:
        """Follow up on a reply that has just been sent.

        Its other matches are forgotten after `forget_after` seconds; with `retire`, the
        "Wrong book?" button is removed then too.
        """
        if kwargs["embeds"]:
            self.spawn(self.ensure_covers(message, kwargs["embeds"], message))
        # An edited reply has new matches, or none; either way the old ones are done with.
        self.replies.pop(message.id, None)
        if reply is None:
            return
        reply.message = message
        self.replies[message.id] = reply
        self.spawn(self._forget_later(message.id, reply, forget_after, retire))

    def spawn(self, coroutine) -> None:
        """Run something in the background, keeping a reference so it isn't dropped."""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def ensure_covers(
        self, message: discord.Message, embeds: list[discord.Embed], shown: discord.Message
    ) -> None:
        """Re-send a message's cards until Discord shows the covers they were given.

        Discord fetches a cover itself the first time it sees its URL, so that it can
        serve the image from its own servers, and is supposed to fill the cover in when
        that fetch finishes. Sometimes it doesn't, for new messages and reliably for
        edited ones: the card stays coverless, even after reloading. In the app this
        looks like the cover flashing and vanishing: the app first draws the card as
        sent, then Discord's processed version, without the cover, replaces it.

        Sending the same cards again once Discord has the image fixes it, which is also
        why switching to another book and back used to make a cover appear. So after each
        wait we look at Discord's copy of the message, and re-send only if a cover is
        still missing. None of this is documented by Discord; it was worked out by
        observation. If it stops holding, this quietly does nothing.

        `embeds` is the live list for the reply, so a card swapped in the meantime is
        re-sent as it is now. `shown` is Discord's copy of the message as last seen.
        """
        for delay in COVER_RETRY_DELAYS:
            if not _covers_missing(embeds, shown):
                return
            await asyncio.sleep(delay)
            try:
                shown = await message.fetch()
                if _covers_missing(embeds, shown):
                    shown = await message.edit(embeds=embeds)
            except discord.HTTPException:
                return

    async def _forget_later(
        self, message_id: int, reply: Reply, after: float, retire: bool
    ) -> None:
        await asyncio.sleep(after)
        if self.replies.get(message_id) is not reply:
            return  # deleted, or replaced when the message was edited
        del self.replies[message_id]
        if not retire:
            return
        try:
            await reply.message.edit(view=reply_view(reply.requester_id))
        except discord.HTTPException:
            pass  # reply was deleted, or the edit window for a slash reply closed

    # --- {curly brace} messages ---------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        # Everything that can fail sits behind these checks, so ordinary chat messages
        # can never produce an error reply or reaction.
        if message.author.bot:
            return
        queries = extract_queries(message.content)
        if not queries:
            return

        async with self._one_at_a_time(message.id):
            try:
                await self._answer(message, queries)
            except Exception as error:
                await self._failed(message, f"this message: {message.jump_url}", error)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # As in on_message, ordinary chat never gets past these checks. The lock is
        # checked too because a message can be edited while its first reply is still
        # being put together, before there is anything in `answered`.
        message = payload.message
        if message.author.bot:
            return
        queries = extract_queries(message.content)
        if not queries and message.id not in self.answered and message.id not in self._locks:
            return

        async with self._one_at_a_time(message.id):
            try:
                await self._answer_edit(message, queries)
            except Exception as error:
                await self._failed(message, f"an edit to this message: {message.jump_url}", error)

    @contextlib.asynccontextmanager
    async def _one_at_a_time(self, message_id: int):
        """Make whatever else is handling the same message wait its turn.

        People often fix a typo within seconds, so an edit can arrive while the lookups
        for the original message are still running. Without this, both would reply.
        """
        lock, users = self._locks.get(message_id) or (asyncio.Lock(), 0)
        self._locks[message_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[message_id]
            if users == 1:
                del self._locks[message_id]
            else:
                self._locks[message_id] = (lock, users - 1)

    async def _failed(self, message: discord.Message, where: str, error: Exception) -> None:
        try:
            await message.add_reaction(self.error_reaction)
        except discord.HTTPException:
            pass
        await self.report_error(where, error)

    async def report_gaps(
        self, cards: list[Card], where: str, books_retryable: bool, covers_retryable: bool
    ) -> None:
        """DM the owner about the cards that call for adding to Hardcover.

        That is a query that found nothing, and a book whose Hardcover page lacks its cover
        or blurb, which is why its card's title links to Goodreads or Amazon instead. The
        two `retryable`s say whether /retry-missed can put each kind right in the reply.
        """
        if self._owner_id is None:
            return
        now = time.monotonic()
        self._last_gap_dm = {
            key: last for key, last in self._last_gap_dm.items() if now - last < GAP_DM_INTERVAL
        }
        for card in cards:
            key = f"book:{card.book.id}" if card.book else f"query:{card.query.casefold()}"
            if key in self._last_gap_dm:
                continue
            if card.book and card.book.is_incomplete:
                text = gap_text(card.book, where, covers_retryable)
            elif card.book is None:
                text = missing_text(card.query, where, books_retryable)
            else:
                continue
            self._last_gap_dm[key] = now
            await self.tell_owner(text)

    async def _answer(
        self,
        message: discord.Message,
        queries: list[str],
        known: dict[str, Card] | None = None,
        logged: dict[str, int] | None = None,
        sent: discord.Message | None = None,
        embeds: list[discord.Embed] | None = None,
        replace_only: bool = False,
    ) -> bool:
        """Reply to a message, or with `sent`, change that existing reply to fit it.

        `known` and `logged` are as in _look_up, and `embeds` as in _compose. With
        `replace_only`, a reply that has been deleted stays deleted, and this returns False.
        """
        where = f"{place_text(message.guild, message.channel)}: {message.jump_url}"
        cards = await self._look_up(
            queries, message_origin(message), message.content, known, logged, where
        )
        kwargs, reply = await self._compose(cards, message.author.id, embeds)
        if sent is not None:
            try:
                # An edit leaves alone whatever it isn't given, so the text and buttons
                # have to be cleared explicitly when the new reply has none.
                sent = await sent.edit(**{"content": None, "view": None, **kwargs})
            except discord.NotFound:
                if replace_only:
                    return False
                sent = None  # the reply was deleted; start a new one
        if sent is None:
            sent = await message.reply(mention_author=False, **kwargs)
        self._sent(reply, kwargs, sent, MATCHES_REMEMBERED_FOR)

        self.answered.pop(message.id, None)  # so that it counts as the newest
        self.answered[message.id] = Answered(cards, sent, kwargs["embeds"])
        while len(self.answered) > REMEMBERED_MESSAGES:
            del self.answered[next(iter(self.answered))]
        return True

    async def _answer_edit(self, message: discord.Message, queries: list[str]) -> None:
        """Bring the reply to an edited message in line with the queries it has now."""
        wanted = [query.casefold() for query in queries]
        answered = self.answered.get(message.id)
        if answered:
            known = {card.query.casefold(): card for card in answered.cards}
            if wanted == list(known):
                return  # the edit didn't touch the queries, or wasn't really an edit
            logged = {key: card.mention_id for key, card in known.items() if card.mention_id}
            sent, embeds = answered.message, answered.embeds
        else:
            # Either the message had no queries until now, or the bot has forgotten its
            # reply (see REMEMBERED_MESSAGES). The channel and the usage log say which.
            if not queries or not _just_edited(message):
                return
            known, embeds = {}, None
            logged = await self.logged_mentions(message)
            sent = await self._find_reply(message)

        await self.forget_usage([id for key, id in logged.items() if key not in wanted])
        if queries:
            await self._answer(message, queries, known, logged, sent, embeds)
            return
        del self.answered[message.id]
        self.replies.pop(sent.id, None)
        try:
            await sent.delete()
        except discord.NotFound:
            pass

    async def _find_reply(self, message: discord.Message) -> discord.Message | None:
        """Look through the channel for the bot's reply to a message."""
        try:
            async for later in message.channel.history(after=message, limit=REPLY_SEARCH_LIMIT):
                replied_to = later.reference.message_id if later.reference else None
                if later.author.id == self.user.id and replied_to == message.id:
                    return later
        except discord.HTTPException as error:
            log.warning("Could not look for the reply to %s: %s", message.jump_url, error)
        return None

    async def restore_reply(self, sent: discord.Message, requester_id: int) -> Reply | None:
        """Work out again what "Wrong book?" offers for a reply the bot has forgotten.

        The message the reply answers still has the queries, so they are searched for
        again; the reply's own cards say which book each one shows now, since a card's
        links line has its Hardcover link. The cards are left as they are, which keeps a
        book picked earlier. The search may not give the matches it gave the first time.

        None when that can't be done: for a /book reply, when the message was deleted,
        when its queries no longer line up with the cards (it was edited while the bot
        was down, or a query finds nothing now), or when no card has another match.
        """
        asked_id = sent.reference.message_id if sent.reference else None
        if asked_id is None:
            return None
        async with self._one_at_a_time(asked_id):
            reply = self.replies.get(sent.id)
            if reply is not None:
                return reply  # put back by another click in the meantime
            answered = self.answered.get(asked_id)
            if answered and answered.message.id == sent.id:
                # Only the matches' hour has passed: the cards are still here, and using
                # those same ones keeps a later edit to the message in step with a pick.
                sent = answered.message
                _, reply = await self._compose(answered.cards, requester_id, answered.embeds)
            else:
                reply = await self._search_again(sent, asked_id, requester_id)
            if reply is None:
                return None
            reply.message = sent
            self.replies[sent.id] = reply
            self.spawn(self._forget_later(sent.id, reply, MATCHES_REMEMBERED_FOR, False))
            return reply

    async def _search_again(
        self, sent: discord.Message, asked_id: int, requester_id: int
    ) -> Reply | None:
        try:
            asked = await sent.channel.fetch_message(asked_id)
        except discord.NotFound:
            return None
        # No origin, so nothing is written to the usage log: these lookups were counted
        # when the message was first answered. `logged` finds those records again, so
        # that a switched book can still be corrected in them.
        logged = await self.logged_mentions(asked)
        cards = await self._look_up(extract_queries(asked.content), logged=logged)
        found = [card for card in cards if card.book]
        embeds = list(sent.embeds)
        if len(found) != len(embeds):
            return None
        menus: list[tuple[int, list[Book]]] = []
        selected: dict[int, int] = {}
        for index, (card, embed) in enumerate(zip(found, embeds, strict=True)):
            shown_id = hardcover_book_id(embed.description or "")
            if shown_id is None:
                return None
            card.embed = embed
            # If the search no longer returns the book shown, the card keeps it and the
            # menu offers the current matches with none marked as chosen.
            card.book = next((b for b in card.alternatives if b.id == shown_id), card.book)
            if any(book.id != shown_id for book in card.alternatives):
                menus.append((index, card.alternatives))
                selected[index] = shown_id
        if not menus:
            return None
        mention_ids = {
            index: card.mention_id for index, card in enumerate(found) if card.mention_id
        }
        return Reply(requester_id, embeds, menus, selected, mention_ids, found)

    # --- /book ----------------------------------------------------------------------------

    async def handle_command(self, interaction: discord.Interaction, query: str) -> None:
        # A quick lookup is answered with the card itself. A slow one has to be answered
        # with "thinking…" first, which the card then replaces. Discord treats that as an
        # edit, tends to drop the cover from it, and the re-send that brings the cover
        # back (see ensure_covers) leaves the reply marked "(edited)".
        where = f"/book in {place_text(interaction.guild, interaction.channel)}"

        async def respond():
            tip = asyncio.create_task(self._wants_tip(interaction))
            cards = await self._look_up([query], command_origin(interaction), where=where)
            kwargs, reply = await self._compose(cards, interaction.user.id)
            if await tip:
                # Later edits to the reply only change its cards and buttons, so it stays.
                kwargs["content"] = "\n".join(filter(None, [kwargs.get("content"), BOOK_TIP]))
            return cards, kwargs, reply

        lookup = asyncio.create_task(respond())
        done, _ = await asyncio.wait({lookup}, timeout=DIRECT_ANSWER_WITHIN)
        if done:
            cards, kwargs, reply = lookup.result()
            await interaction.response.send_message(**kwargs)
            sent = await interaction.original_response()
        else:
            await interaction.response.defer()
            cards, kwargs, reply = await lookup
            sent = await interaction.followup.send(wait=True, **kwargs)
        self._sent(reply, kwargs, sent, COMMAND_REPLY_EXPIRES_AFTER, retire=True)
        # Nothing else records where a /book reply is, and /retry-missed needs to know.
        mention_ids = [card.mention_id for card in cards if card.mention_id]
        if mention_ids:
            self.spawn(self.record_reply(mention_ids, sent.id))

    async def _wants_tip(self, interaction: discord.Interaction) -> bool:
        """Whether a /book reply should tell its asker about {…}.

        Only in a server that has added the bot, since {…} works nowhere else, and only
        in a channel the bot can see. There, each person is told once, as long as nobody
        in the server has used {…}. Never raises: the reply matters more than the tip.
        """
        guild = interaction.guild
        if not (self.usage and guild and interaction.is_guild_integration()):
            return False
        if guild.id in self._braces_servers or not interaction.app_permissions.view_channel:
            return False
        try:
            claimed = await self.usage.claim_tip(guild.id, interaction.user.id)
        except Exception as error:
            await self.report_error("checking whether to show the {…} tip", error)
            return False
        if claimed is None:
            self._braces_servers.add(guild.id)
        return bool(claimed)

    # --- /help -----------------------------------------------------------------------------

    async def handle_help(self, interaction: discord.Interaction) -> None:
        """Explain the bot, to the person who asked only."""
        await interaction.response.send_message(
            help_text(interaction.application_id, self.config.support_url), ephemeral=True
        )

    # --- /stats ----------------------------------------------------------------------------

    async def handle_stats(
        self,
        interaction: discord.Interaction,
        server: str | None,
        exclude: str | None,
        all_servers: bool = False,
        owner: str = "leave out",
    ) -> None:
        """Show the owner the report `python -m bookbot.stats` prints, as Discord messages.

        Its tables are too wide for a phone, so each row is a line of its own instead.
        The answer is an ordinary message, not one only the owner can see: the command only
        works in a DM with the bot, which nobody else can see anyway, and an ordinary
        message stays in the DM, so earlier reports can be scrolled back to.
        """
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the bot's owner can use this.", ephemeral=True
            )
            return
        if self.usage is None or not self.config.usage_db:
            await interaction.response.send_message(
                "This needs the usage log, which is what the report summarizes.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        include, leave_out = _server_list(server), _server_list(exclude)
        try:
            # It reads the log and asks Discord over plain HTTP, so not on the event loop.
            report = await asyncio.to_thread(
                gather,
                self.config.usage_db,
                include,
                leave_out,
                all_servers=all_servers,
                owner=hash_user(self.config.usage_salt, self._owner_id),
                owner_mode=owner,
                token=self.config.discord_token,
            )
        except Exception as error:
            await self.report_error("making the stats report", error)
            await interaction.edit_original_response(content="Could not make the report.")
            return
        first, *rest = stats_messages(report)
        await interaction.edit_original_response(content=first)
        for text in rest:
            await interaction.followup.send(text)

    # --- /retry-missed --------------------------------------------------------------------

    async def handle_retry(
        self, interaction: discord.Interaction, hours: int, dry_run: bool = False
    ) -> None:
        """Search again for what recent messages asked for and didn't get.

        For the owner, after adding the missing books to Hardcover: each reply that said
        it couldn't find a book is changed to show it. Likewise, after filling in a cover
        or blurb on Hardcover, each card that lacked one gets the cover, and its title
        links to Hardcover again. That includes /book replies, but a /book that found
        nothing is left as it is: a card put into it would need the asker's ID for its
        buttons, and the log doesn't keep that.

        With `dry_run`, every reply is found as it would be, but nothing is changed, so
        the owner can check what a real run would do, even in servers they aren't in.
        """
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "Only the bot's owner can use this.", ephemeral=True
            )
            return
        if self.usage is None:
            await interaction.response.send_message(
                "This needs the usage log, which is where missed queries are kept.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        by_message: dict[int, list[Miss]] = {}
        for miss in await self.usage.misses(hours):
            by_message.setdefault(miss.message_id, []).append(miss)
        incomplete_by_message: dict[int, list[Incomplete]] = {}
        for entry in await self.usage.incomplete(hours):
            # A /book has one card and no message; an old one has no reply ID either.
            key = entry.message_id or entry.reply_id or entry.mention_id
            incomplete_by_message.setdefault(key, []).append(entry)
        total = sum(len(misses) for misses in by_message.values())
        incomplete = sum(len(entries) for entries in incomplete_by_message.values())
        parts = []
        if by_message or not incomplete_by_message:
            parts.append(
                f"{_count(total, 'query', 'queries')} that found nothing, "
                f"in {_count(len(by_message), 'reply', 'replies')}"
            )
        if incomplete_by_message:
            parts.append(
                f"{_count(incomplete, 'book', 'books')} without a cover or blurb, "
                f"in {_count(len(incomplete_by_message), 'reply', 'replies')}"
            )
        scope = f"{', and '.join(parts)}, from the last {_count(hours, 'hour', 'hours')}"
        log.info("Retrying %s%s", scope, " (dry run)" if dry_run else "")
        if not (by_message or incomplete_by_message):
            await interaction.edit_original_response(content=f"Nothing to retry: {scope}.")
            return
        await interaction.edit_original_response(content=f"Looking again for {scope}…")

        # Hardcover is asked first, and once per distinct query or book. Discord only
        # comes into it for a message that has something new to show.
        results: dict[str, LookupResult] = {}
        for misses in by_message.values():
            for miss in misses:
                key = miss.query.casefold()
                if key not in results:
                    results[key] = await find_book(self.hardcover, miss.query)
        book_ids = sorted({e.book_id for es in incomplete_by_message.values() for e in es})
        now: dict[int, Book] = {}
        if book_ids:
            try:
                now = await self.hardcover.covers_and_blurbs(book_ids)
            except HardcoverError as error:
                await self.report_error("looking up covers and blurbs", error)
        filled = {book_id: book for book_id, book in now.items() if not book.is_incomplete}

        # The books put into each reply changed, by its link. A reply can be changed both
        # ways, for a book found now and another's cover.
        # Where each reply is, by name, for a server the owner isn't in.
        places: dict[str, str] = {}
        # The channels of the replies left alone, by ID.
        unreached: set[int] = set()

        def tally(shown, first: Miss | Incomplete) -> None:
            if shown is None:
                unreached.add(first.thread_id or first.channel_id)
                return
            reply, books = shown
            updated.setdefault(reply.jump_url, []).extend(books)
            places[reply.jump_url] = place_text(reply.guild, reply.channel)

        updated: dict[str, list[Book]] = {}
        left_alone = 0
        for misses in by_message.values():
            if not any(results[miss.query.casefold()].book for miss in misses):
                continue
            try:
                shown = await self._retry_message(misses, results, dry_run)
            except Exception as error:
                await self.report_error("retrying a missed query", error)
                shown = None
            left_alone += shown is None
            tally(shown and (shown[0], [card.book for card in shown[1]]), misses[0])
        for entries in incomplete_by_message.values():
            if not any(entry.book_id in filled for entry in entries):
                continue
            try:
                shown = await self._refresh_message(entries, filled, dry_run)
            except Exception as error:
                await self.report_error("filling in a cover or blurb", error)
                shown = None
            left_alone += shown is None
            tally(shown, entries[0])

        lines = [
            "Dry run: nothing was changed." if dry_run else "",
            f"Looked again for {scope}.",
        ]
        if updated:
            done = "Would update" if dry_run else "Updated"
            lines.append(f"**{done} {_count(len(updated), 'reply', 'replies')}:**")
            lines += [
                f"- {_titles(books)} ({places[url]}): {url}"
                for url, books in list(updated.items())[:RETRIES_LISTED]
            ]
            if len(updated) > RETRIES_LISTED:
                lines.append(f"- and {len(updated) - RETRIES_LISTED} more")
        if left_alone:
            lines.append(
                f"**{'Would leave' if dry_run else 'Left'}"
                f" {_count(left_alone, 'reply', 'replies')} alone** that could be "
                "updated now, because the message or the reply is gone or can't be reached."
            )
            lines += [
                f"- The bot lacks {' and '.join(lacking)} in {where}"
                for where, lacking in self._missing_permissions(unreached)
            ]
        missing = sorted(
            {
                miss.query
                for misses in by_message.values()
                for miss in misses
                if not results[miss.query.casefold()].book
            }
        )
        if missing:
            names = discord.utils.escape_markdown(", ".join(missing))
            lines.append(f"**Still not found ({len(missing)}):** {names}")
        lacking = sorted({book.title for book in now.values() if book.is_incomplete})
        if lacking:
            names = discord.utils.escape_markdown(", ".join(lacking))
            lines.append(f"**Still without a cover or blurb ({len(lacking)}):** {names}")
        text = "\n".join(line for line in lines if line)
        log.info("Retried %s: %d updated, %d left alone", scope, len(updated), left_alone)
        try:
            await interaction.edit_original_response(
                content=text if len(text) <= 2000 else text[:1999] + "…"
            )
        except discord.HTTPException:
            # More than 15 minutes went by, so Discord no longer takes the answer.
            log.warning("Could not report on the retry: %s", text)

    async def _retry_message(
        self, misses: list[Miss], results: dict[str, LookupResult], dry_run: bool = False
    ) -> tuple[discord.Message, list[Card]] | None:
        """Put the books now found for a message's missed queries into its reply.

        Returns the reply and the new cards, or None if nothing was changed: the message
        or the reply is gone, or the queries have been edited out since. With `dry_run`,
        the same, but the reply is left as it is.
        """
        message_id = misses[0].message_id
        async with self._one_at_a_time(message_id):
            answered = self.answered.get(message_id)
            message = await self._fetch_asked(misses[0], answered)
            if message is None:
                return None
            queries = extract_queries(message.content)
            wanted = {query.casefold() for query in queries}
            if answered:
                known = {card.query.casefold(): card for card in answered.cards if card.book}
                logged = {
                    card.query.casefold(): card.mention_id
                    for card in answered.cards
                    if card.mention_id
                }
                sent, embeds = answered.message, answered.embeds
            else:
                sent, embeds = await self._find_reply(message), None
                if sent is None:
                    return None
                logged = await self.logged_mentions(message)
                missed = {miss.query.casefold() for miss in misses}
                known = await self._shown_cards(
                    [query for query in queries if query.casefold() not in missed], sent, logged
                )
            # The searches for the missed queries are done, so their cards are handed
            # over ready-made, including the ones that still find nothing.
            retried: dict[str, Card] = {}
            for miss in misses:
                key = miss.query.casefold()
                if key in wanted and key not in known:
                    result = results[key]
                    retried[key] = Card(
                        miss.query, result.book, result.alternatives, mention_id=miss.mention_id
                    )
            found = [card for card in retried.values() if card.book]
            if not found:
                return None
            if dry_run:
                return sent, found
            if not await self._answer(
                message, queries, known | retried, logged, sent, embeds, replace_only=True
            ):
                return None
            for card in found:
                await self.record_found(card)
            return self.answered[message_id].message, found

    async def _refresh_message(
        self, entries: list[Incomplete], filled: dict[int, Book], dry_run: bool = False
    ) -> tuple[discord.Message, list[Book]] | None:
        """Fill in the cards of a message's reply whose books now have a cover and blurb.

        `filled` has those books, by ID, as Hardcover has them now. Returns the reply and
        the books filled in, or None if nothing was changed: the message or the reply is
        gone, or its cards show other books by now. `entries` are for one message, or for
        one /book reply. With `dry_run`, the same, but the reply is left as it is.
        """
        first = entries[0]
        async with self._one_at_a_time(first.message_id or first.reply_id or first.mention_id):
            answered = self.answered.get(first.message_id) if first.message_id else None
            live = None
            if answered:
                sent, embeds = answered.message, answered.embeds
            else:
                if first.message_id:
                    message = await self._fetch_asked(first, None)
                    sent = message and await self._find_reply(message)
                else:
                    sent = await self._fetch_command_reply(first)
                if sent is None:
                    return None
                # A /book reply can still have "Wrong book?", whose cards these are.
                live = self.replies.get(sent.id)
                embeds = live.embeds if live else list(sent.embeds)
            # The cards to fill in, with their books. `embeds` may be the live list, which
            # a dry run must leave alone.
            to_fill = [
                (embed, filled[book_id])
                for embed in embeds
                if (book_id := hardcover_book_id(embed.description or "")) in filled
            ]
            books = [book for _, book in to_fill]
            if not books:
                return None
            if dry_run:
                return sent, books
            for embed, book in to_fill:
                fill_in_card(embed, book)
            try:
                shown = await sent.edit(embeds=embeds)
            except discord.NotFound:
                return None
            if answered:
                answered.message = shown
            # So that a card made again from these, as when a book is switched back with
            # "Wrong book?", keeps the cover too.
            for card in answered.cards if answered else live.cards if live else []:
                if card.book:
                    card.book = _with_cover_and_blurb(card.book, filled)
                card.alternatives[:] = [
                    _with_cover_and_blurb(book, filled) for book in card.alternatives
                ]
            self.spawn(self.ensure_covers(shown, embeds, shown))
            await self.usage.completed(
                [entry.mention_id for entry in entries if entry.book_id in filled]
            )
            return shown, books

    async def _fetch_asked(
        self, miss: Miss | Incomplete, answered: Answered | None
    ) -> discord.Message | None:
        """The message a missed query was in, or None if it can't be had any more."""
        try:
            if answered:
                places = [answered.message.channel]
            elif miss.thread_id:
                thread_id = miss.thread_id
                places = [self.get_channel(thread_id) or await self.fetch_channel(thread_id)]
            else:
                channel = self.get_channel(miss.channel_id)
                # The newest threads that already existed when the message was sent.
                threads = sorted(
                    (t for t in getattr(channel, "threads", []) if t.id <= miss.message_id),
                    key=lambda thread: thread.id,
                    reverse=True,
                )
                places = [channel, *threads[:OLD_THREADS_TRIED]]
        except discord.HTTPException:
            return None
        for place in places:
            # A forum has no messages of its own, and a channel the bot has left is None.
            fetch = getattr(place, "fetch_message", None)
            if fetch is None:
                continue
            try:
                return await fetch(miss.message_id)
            except (discord.NotFound, discord.Forbidden):
                continue
        return None

    def _missing_permissions(self, channel_ids: set[int | None]) -> list[tuple[str, list[str]]]:
        """Each of these channels where the bot lacks what it needs to reach its replies,
        by name, with what it lacks. Only channels the bot can still see are checked."""
        found = []
        for channel_id in sorted(filter(None, channel_ids)):
            channel = self.get_channel(channel_id)
            guild = getattr(channel, "guild", None)
            if guild is None or guild.me is None:
                continue
            allowed = channel.permissions_for(guild.me)
            lacking = [
                name
                for name, ok in (
                    ("View Channel", allowed.view_channel),
                    ("Read Message History", allowed.read_message_history),
                )
                if not ok
            ]
            if lacking:
                found.append((place_text(guild, channel), lacking))
        return found

    async def _fetch_command_reply(self, entry: Incomplete) -> discord.Message | None:
        """A /book reply, or None if it can't be had any more.

        A record from before reply IDs were kept is matched by looking through the channel
        from shortly before the lookup for the bot's /book reply showing the book.
        """
        place_id = entry.thread_id or entry.channel_id
        try:
            place = self.get_channel(place_id) or await self.fetch_channel(place_id)
            if not hasattr(place, "history"):  # a forum, whose posts are threads
                return None
            if entry.reply_id:
                return await place.fetch_message(entry.reply_id)
            # A slow lookup is answered by editing a "thinking…" sent before it was logged.
            since = datetime.datetime.fromisoformat(entry.mentioned_at) - OLD_REPLY_LEEWAY
            async for later in place.history(after=since, limit=REPLY_SEARCH_LIMIT):
                if (
                    later.author.id == self.user.id
                    and later.interaction_metadata is not None
                    and any(
                        hardcover_book_id(embed.description or "") == entry.book_id
                        for embed in later.embeds
                    )
                ):
                    return later
        except discord.HTTPException:
            return None
        return None

    async def _shown_cards(
        self, queries: list[str], sent: discord.Message, logged: dict[str, int]
    ) -> dict[str, Card]:
        """Cards for the books a forgotten reply shows, by lowercased query.

        Each keeps the card that is there, and with it a book picked with "Wrong book?".
        Empty if the queries don't line up with the reply's cards, or a card shows a book
        its query no longer finds; the reply is then put together from scratch.
        """
        if len(queries) != len(sent.embeds):
            return {}
        cards: dict[str, Card] = {}
        for query, embed in zip(queries, sent.embeds, strict=True):
            shown_id = hardcover_book_id(embed.description or "")
            result = await find_book(self.hardcover, query)
            book = next((b for b in result.alternatives if b.id == shown_id), None)
            if book is None:
                return {}
            key = query.casefold()
            cards[key] = Card(query, book, result.alternatives, embed, logged.get(key))
        return cards

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        text = f"{self.error_reaction} Something went wrong looking that up. Try again later."
        name = interaction.command.name if interaction.command else "book"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass
        channel = place_text(interaction.guild, interaction.channel)
        original = getattr(error, "original", error)
        await self.report_error(f"the /{name} command in {channel}", original)

    # --- Telling the owner when things break ----------------------------------------------

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        log.exception("Unhandled error in %s", event_method)

    async def report_error(self, where: str, error: Exception) -> None:
        """Log an error and DM the owner, at most once an hour per distinct error."""
        log.error("Error handling %s", where, exc_info=error)
        # Throttled per distinct error, so that a Hardcover outage produces one DM rather
        # than one per message, while a different problem still gets through.
        key = error_key(error)
        now = time.monotonic()
        last = self._last_owner_dm.get(key)
        if last is not None and now - last < OWNER_DM_INTERVAL:
            return
        self._last_owner_dm[key] = now
        details = "".join(traceback.format_exception_only(error)).strip()
        await self.tell_owner(
            f"{ERROR_REACTION} Book bot hit an error handling {where}\n```\n{details[:1500]}\n```"
            "Repeats of this error are muted for an hour; the logs have all of them."
        )

    async def tell_owner(self, text: str) -> None:
        """DM the owner. Never raises: there is nobody else to tell if this fails."""
        try:
            owner = self.get_user(self._owner_id) or await self.fetch_user(self._owner_id)
            await owner.send(text)
        except Exception:
            log.exception("Could not DM the owner: %s", text)

    # --- Telling the owner where the bot is -----------------------------------------------

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._server_changed("Added to", guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._server_changed("Removed from", guild)

    async def _server_changed(self, what: str, guild: discord.Guild) -> None:
        log.info("%s server %s (%s)", what, guild.name, guild.id)
        members = f", which has {guild.member_count} members" if guild.member_count else ""
        await self.tell_owner(
            f"{what} the server **{discord.utils.escape_markdown(guild.name)}**{members}. "
            f"The bot is now in {_count(len(self.guilds), 'server', 'servers')}."
        )

    # --- Heartbeat ------------------------------------------------------------------------

    @tasks.loop(minutes=HEARTBEAT_MINUTES)
    async def heartbeat(self) -> None:
        """Ping an external monitor, which alerts the owner if the pings stop."""
        # Skipped while disconnected from Discord, so that the monitor raises the alarm
        # when the process is alive but the bot isn't actually listening.
        if not self.is_ready() or self.is_closed():
            return
        try:
            async with self._session.get(
                self.config.heartbeat_url, timeout=aiohttp.ClientTimeout(total=10)
            ):
                pass
        except (aiohttp.ClientError, TimeoutError) as error:
            log.warning("Heartbeat ping failed: %s", error)

    @heartbeat.before_loop
    async def _before_heartbeat(self) -> None:
        await self.wait_until_ready()
