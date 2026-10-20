"""The reply controls, exercised with stand-ins for Discord's interaction objects."""

from types import SimpleNamespace

import discord
import pytest

import bookbot.bot as botmod
from bookbot.bot import (
    EXPIRED,
    BookBot,
    DeleteButton,
    MatchSelect,
    WrongBookButton,
    picker_view,
)
from bookbot.config import Config
from bookbot.formatting import CARD_COLORS
from bookbot.hardcover import Author, Book
from bookbot.links import Links
from bookbot.lookup import LookupResult
from bookbot.parsing import MAX_QUERIES_PER_MESSAGE

REQUESTER, STRANGER, MESSAGE_ID = 42, 7, 1000

DUNE = Book(id=1, title="Dune", authors=[Author(id=1, name="Frank Herbert")])
DUNE_MESSIAH = Book(id=2, title="Dune Messiah", authors=[Author(id=1, name="Frank Herbert")])
CIRCE = Book(id=3, title="Circe")
RESULTS = {
    "dune": LookupResult(book=DUNE, alternatives=[DUNE, DUNE_MESSIAH]),
    "circe": LookupResult(book=CIRCE, alternatives=[CIRCE]),
    "nothing": LookupResult(book=None),
}


class Recorder:
    """Records calls made to it as (method name, keyword arguments)."""

    def __init__(self, calls, fail=None):
        self.calls, self.fail = calls, fail or {}

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append((name, {**kwargs, **({"args": args} if args else {})}))
            if name in self.fail:
                raise self.fail[name]

        return method


def interaction(bot, user_id, calls, manage_messages=False):
    async def fetch_message(message_id):
        return SimpleNamespace(id=message_id, reference=None)

    return SimpleNamespace(
        client=bot,
        user=SimpleNamespace(id=user_id),
        # As for a /book reply: one to a message has a `reference` (see test_edits.py).
        message=SimpleNamespace(id=MESSAGE_ID, reference=None),
        channel=SimpleNamespace(fetch_message=fetch_message),
        permissions=SimpleNamespace(manage_messages=manage_messages),
        response=Recorder(calls),
        followup=Recorder(calls),
        edit_original_response=Recorder(calls).edit_original_response,
        delete_original_response=Recorder(calls).delete_original_response,
    )


def controls(view):
    return [type(item).__name__ for item in view.children]


@pytest.fixture
async def bot(monkeypatch):
    bot = BookBot(Config(discord_token="x", hardcover_token="y"))

    async def find_book(client, query):
        return RESULTS[query]

    async def links_for(book):
        return Links("https://h", "https://g", "https://a")

    monkeypatch.setattr(botmod, "find_book", find_book)
    bot.hardcover = None
    bot.links_for = links_for
    return bot


async def remembered(bot, queries, fail=None):
    """Build a reply as the bot would and register it as sent; returns (reply, edits)."""
    kwargs, reply = await bot._respond(queries, REQUESTER)
    edits = []
    reply.message = Recorder(edits, fail)
    bot.replies[MESSAGE_ID] = reply
    return reply, edits


async def test_reply_with_other_matches_offers_wrong_book(bot):
    kwargs, reply = await bot._respond(["dune", "nothing", "circe"], REQUESTER)
    assert len(kwargs["embeds"]) == 2
    assert "Couldn't find **nothing**" in kwargs["content"]
    assert controls(kwargs["view"]) == ["WrongBookButton", "DeleteButton"]
    assert reply.menus == [(0, [DUNE, DUNE_MESSIAH])]
    assert reply.selected == {0: DUNE.id}


async def test_several_books_take_the_stripe_colors_in_order(bot):
    kwargs, _ = await bot._respond(["dune", "nothing", "circe"], REQUESTER)
    assert [str(embed.color) for embed in kwargs["embeds"]] == CARD_COLORS[:2]


async def test_a_full_reply_has_a_color_and_a_menu_for_every_book(bot):
    kwargs, reply = await bot._respond(["dune"] * MAX_QUERIES_PER_MESSAGE, REQUESTER)
    assert [str(embed.color) for embed in kwargs["embeds"]] == CARD_COLORS
    assert len(picker_view(MESSAGE_ID, reply).children) == MAX_QUERIES_PER_MESSAGE


async def test_reply_without_other_matches_only_offers_delete(bot):
    kwargs, reply = await bot._respond(["circe"], REQUESTER)
    assert controls(kwargs["view"]) == ["DeleteButton"]
    assert reply is None


async def test_reply_with_nothing_found_has_no_controls(bot):
    kwargs, reply = await bot._respond(["nothing"], REQUESTER)
    assert kwargs["embeds"] == [] and "view" not in kwargs and reply is None


async def test_wrong_book_shows_requester_a_private_menu(bot):
    await remembered(bot, ["dune", "circe"])
    calls = []
    await WrongBookButton(REQUESTER).callback(interaction(bot, REQUESTER, calls))
    [(name, sent)] = calls
    assert name == "send_message" and sent["ephemeral"] is True
    [menu] = sent["view"].children
    assert isinstance(menu, MatchSelect)
    assert menu.item.custom_id == f"bookbot:pick:{MESSAGE_ID}:0"
    assert [(o.label, o.default) for o in menu.item.options] == [
        ("Dune — Frank Herbert", True),
        ("Dune Messiah — Frank Herbert", False),
    ]


async def test_wrong_book_refuses_other_people(bot):
    await remembered(bot, ["dune"])
    calls = []
    await WrongBookButton(REQUESTER).callback(interaction(bot, STRANGER, calls))
    [(name, sent)] = calls
    assert name == "send_message" and sent["ephemeral"] is True and "view" not in sent


async def test_wrong_book_on_a_command_reply_after_restart_explains_and_removes_itself(bot):
    calls = []  # nothing remembered, as after a restart
    await WrongBookButton(REQUESTER).callback(interaction(bot, REQUESTER, calls))
    (edit, edited), (send, sent) = calls
    assert edit == "edit_message" and controls(edited["view"]) == ["DeleteButton"]
    assert send == "send" and sent["args"] == (EXPIRED,) and sent["ephemeral"] is True


async def pick(bot, book, calls):
    menu = picker_view(MESSAGE_ID, bot.replies[MESSAGE_ID]).children[0]
    menu.item._values = [str(book.id)]
    await menu.callback(interaction(bot, REQUESTER, calls))


async def test_picking_a_match_swaps_the_public_card(bot):
    reply, edits = await remembered(bot, ["dune", "circe"])
    calls = []
    await pick(bot, DUNE_MESSIAH, calls)

    [(name, edited)] = edits
    assert name == "edit"
    assert "Dune Messiah" in edited["embeds"][0].title
    assert "Circe" in edited["embeds"][1].title
    assert [str(embed.color) for embed in edited["embeds"]] == CARD_COLORS[:2]
    assert reply.selected == {0: DUNE_MESSIAH.id}
    # The private menu is redrawn with the new choice selected.
    assert [name for name, _ in calls] == ["defer", "edit_original_response"]
    options = calls[1][1]["view"].children[0].item.options
    assert [o.default for o in options] == [False, True]


async def test_picking_on_a_command_reply_after_restart_says_expired(bot):
    menu = MatchSelect(MESSAGE_ID, 0, discord.ui.Select(options=[discord.SelectOption(label="x")]))
    calls = []
    await menu.callback(interaction(bot, REQUESTER, calls))
    assert calls == [
        ("defer", {}),
        ("edit_original_response", {"content": EXPIRED, "view": None}),
    ]


async def test_picking_when_reply_was_deleted_says_so(bot):
    not_found = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")
    await remembered(bot, ["dune"], fail={"edit": not_found})
    calls = []
    await pick(bot, DUNE_MESSIAH, calls)
    assert calls[-1] == (
        "edit_original_response",
        {"content": "That reply has been deleted.", "view": None},
    )
    assert MESSAGE_ID not in bot.replies


async def test_delete_is_for_requester_and_moderators(bot):
    await remembered(bot, ["dune"])
    calls = []
    await DeleteButton(REQUESTER).callback(interaction(bot, STRANGER, calls))
    assert [name for name, _ in calls] == ["send_message"] and MESSAGE_ID in bot.replies

    calls = []
    await DeleteButton(REQUESTER).callback(interaction(bot, STRANGER, calls, manage_messages=True))
    assert [name for name, _ in calls] == ["defer", "delete_original_response"]

    await remembered(bot, ["dune"])
    calls = []
    await DeleteButton(REQUESTER).callback(interaction(bot, REQUESTER, calls))
    assert [name for name, _ in calls] == ["defer", "delete_original_response"]
    assert MESSAGE_ID not in bot.replies


def test_control_ids_match_their_templates():
    for cls, custom_id in (
        (DeleteButton, "bookbot:delete:42"),
        (WrongBookButton, "bookbot:wrong:42"),
        (MatchSelect, "bookbot:pick:1000:3"),
    ):
        assert cls.__discord_ui_compiled_template__.fullmatch(custom_id)


class FlakyCoverMessage:
    """Like Discord: a cover only shows once the cards have been sent `needed` times.

    With `fills_in_late`, Discord's own copy gains the cover between looks, as it
    normally does for a new message, so no re-send should be needed.
    """

    def __init__(self, needed=1, fills_in_late=False):
        self.needed, self.fills_in_late = needed, fills_in_late
        self.sends, self.fetches = 1, 0  # the original send counts

    def _copy(self, embeds, has_cover):
        width = 100 if has_cover else None
        shown = [SimpleNamespace(thumbnail=SimpleNamespace(width=width)) for _ in embeds]
        return SimpleNamespace(embeds=shown)

    def as_sent(self, embeds):
        return self._copy(embeds, self.sends >= self.needed and not self.fills_in_late)

    async def fetch(self):
        self.fetches += 1
        return self._copy(self.embeds, self.fills_in_late or self.sends >= self.needed)

    async def edit(self, embeds):
        self.sends += 1
        return self._copy(embeds, self.sends >= self.needed)


@pytest.fixture
def no_waiting(monkeypatch):
    monkeypatch.setattr(botmod, "COVER_RETRY_DELAYS", (0, 0, 0))


COVER = "https://example.com/cover.jpg"


@pytest.mark.parametrize(("needed", "expected_sends"), [(1, 1), (2, 2), (3, 3), (99, 4)])
async def test_cards_are_resent_until_the_cover_shows(bot, no_waiting, needed, expected_sends):
    embeds = [discord.Embed().set_thumbnail(url=COVER)]
    message = FlakyCoverMessage(needed)
    message.embeds = embeds
    await bot.ensure_covers(message, embeds, message.as_sent(embeds))
    assert message.sends == expected_sends


async def test_cards_are_not_resent_when_discord_fills_the_cover_in_itself(bot, no_waiting):
    embeds = [discord.Embed().set_thumbnail(url=COVER)]
    message = FlakyCoverMessage(fills_in_late=True)
    message.embeds = embeds
    await bot.ensure_covers(message, embeds, message.as_sent(embeds))
    assert (message.sends, message.fetches) == (1, 1)


async def test_cards_are_not_resent_for_a_book_without_a_cover(bot, no_waiting):
    embeds = [discord.Embed()]
    message = FlakyCoverMessage(needed=99)
    message.embeds = embeds
    await bot.ensure_covers(message, embeds, message.as_sent(embeds))
    assert (message.sends, message.fetches) == (1, 0)


async def test_resend_uses_the_cards_as_they_are_now(bot, no_waiting):
    embeds = [discord.Embed(description="first").set_thumbnail(url=COVER)]
    message = FlakyCoverMessage(needed=2)
    message.embeds = embeds
    sent = []

    async def edit(embeds):
        sent.append(embeds[0].description)
        return message._copy(embeds, True)

    message.edit = edit
    shown = message.as_sent(embeds)
    embeds[0] = discord.Embed(description="swapped").set_thumbnail(url=COVER)
    await bot.ensure_covers(message, embeds, shown)
    assert sent == ["swapped"]


def test_error_key_groups_the_same_failure_and_separates_different_ones():
    from bookbot.bot import error_key
    from bookbot.hardcover import HardcoverError

    unauthorized = HardcoverError('HTTP 401: {"error":"invalid_token"}')
    assert error_key(unauthorized) == error_key(HardcoverError('HTTP 401: {"error":"other"}'))
    assert error_key(unauthorized) != error_key(HardcoverError("HTTP 500: oops"))
    assert error_key(unauthorized) != error_key(ValueError('HTTP 401: {"error":"invalid_token"}'))
    assert error_key(HardcoverError("search returned no results object for 'Dune'")) == error_key(
        HardcoverError("search returned no results object for 'Circe'")
    )


async def test_owner_is_dmed_once_per_distinct_error(bot):
    from bookbot.hardcover import HardcoverError

    sent = []
    bot._owner_id = 1
    bot.get_user = lambda _id: Recorder(sent)
    await bot.report_error(
        "this message: https://discord.com/channels/1/2/3", HardcoverError("HTTP 401: a")
    )
    await bot.report_error(
        "this message: https://discord.com/channels/1/2/4", HardcoverError("HTTP 401: a")
    )
    await bot.report_error("the /book command in <#2>", HardcoverError("HTTP 500: b"))
    texts = [call["args"][0] for _name, call in sent]
    assert len(texts) == 2
    assert "https://discord.com/channels/1/2/3" in texts[0] and "HTTP 401" in texts[0]
    assert "HTTP 500" in texts[1]


@pytest.mark.parametrize("forget_after", [botmod.MATCHES_REMEMBERED_FOR, 0.01])
@pytest.mark.parametrize("retire", [True, False])
async def test_matches_are_forgotten_and_only_a_command_reply_loses_wrong_book(
    bot, forget_after, retire
):
    import asyncio

    kwargs, reply = await bot._respond(["dune"], REQUESTER)
    edits = []
    message = Recorder(edits)
    message.id, message.embeds = MESSAGE_ID, []
    bot._sent(reply, kwargs, message, forget_after, retire)
    await asyncio.sleep(0.05)
    if forget_after > 1:
        assert MESSAGE_ID in bot.replies and edits == []
    elif retire:
        assert MESSAGE_ID not in bot.replies
        [(name, edited)] = edits
        assert name == "edit" and controls(edited["view"]) == ["DeleteButton"]
    else:
        assert MESSAGE_ID not in bot.replies and edits == []
    for task in bot._background_tasks:
        task.cancel()


def test_command_replies_expire_before_discord_stops_allowing_edits():
    assert botmod.COMMAND_REPLY_EXPIRES_AFTER < 15 * 60


async def test_help_is_private_and_links_to_installing_and_support():
    calls = []
    bot = BookBot(
        Config(discord_token="x", hardcover_token="y", support_url="https://discord.gg/abc")
    )
    await bot.handle_help(SimpleNamespace(application_id=123, response=Recorder(calls)))

    [(name, kwargs)] = calls
    text = kwargs["args"][0]
    assert name == "send_message" and kwargs["ephemeral"] is True
    assert "<https://discord.com/oauth2/authorize?client_id=123>" in text
    assert text.endswith("<https://discord.gg/abc>")
    assert f"Up to {MAX_QUERIES_PER_MESSAGE} books" in text


def test_help_leaves_out_support_when_there_is_no_server():
    assert "Questions" not in botmod.help_text(123, None)
