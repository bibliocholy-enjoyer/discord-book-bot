"""Edited messages: the reply follows the queries, in place, and so does the usage log.

Also "Wrong book?" on a reply whose matches the bot has forgotten, which goes back to the
message in the same way.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

import bookbot.bot as botmod
from bookbot.bot import EXPIRED, BookBot, WrongBookButton, picker_view
from bookbot.config import Config
from bookbot.formatting import CARD_COLORS
from bookbot.hardcover import Author, Book
from bookbot.links import Links, hardcover_book_url
from bookbot.lookup import LookupResult
from tests.test_bot import Recorder

REQUESTER, BOT = 42, 99

DUNE = Book(id=1, title="Dune", authors=[Author(id=1, name="Frank Herbert")])
DUNE_MESSIAH = Book(id=2, title="Dune Messiah", authors=[Author(id=1, name="Frank Herbert")])
CIRCE = Book(id=3, title="Circe")
PIRANESI = Book(id=4, title="Piranesi", description="A house of halls.", cover_url="https://c")
RESULTS = {
    "dune": LookupResult(book=DUNE, alternatives=[DUNE, DUNE_MESSIAH]),
    "circe": LookupResult(book=CIRCE, alternatives=[CIRCE]),
    "piranesi": LookupResult(book=PIRANESI, alternatives=[PIRANESI]),
    "nothing": LookupResult(book=None),
}
NOT_FOUND = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


class FakeReply:
    """The bot's reply as Discord would hold it: `shown` is what it currently displays."""

    count = 0

    def __init__(self, replied_to, kwargs):
        FakeReply.count += 1
        self.id = 1000 + FakeReply.count
        self.author = SimpleNamespace(id=BOT)
        self.reference = SimpleNamespace(message_id=replied_to)
        self.shown, self.edits, self.deleted = dict(kwargs), 0, False
        self.jump_url = f"https://discord.com/channels/10/20/{self.id}"

    @property
    def embeds(self):
        return self.shown["embeds"]

    async def edit(self, **kwargs):
        if self.deleted:
            raise NOT_FOUND
        self.edits += 1
        self.shown.update(kwargs)
        return self

    async def delete(self):
        if self.deleted:
            raise NOT_FOUND
        self.deleted = True

    def titles(self):
        return [embed.title.split("**")[1] for embed in self.shown["embeds"]]


class FakeMessage:
    def __init__(self, content, id=500):
        self.id, self.content, self.edited_at, self.deleted = id, content, None, False
        self.author = SimpleNamespace(id=REQUESTER, bot=False)
        self.guild = SimpleNamespace(id=10, name="RfM")
        self.jump_url = f"https://discord.com/channels/10/20/{id}"
        self.replies: list[FakeReply] = []
        self.reactions = []
        self.channel = SimpleNamespace(
            id=20,
            name="general",
            parent=None,
            category=None,
            history=self._history,
            fetch_message=self._fetch_message,
        )

    async def reply(self, mention_author=False, **kwargs):
        self.replies.append(FakeReply(self.id, kwargs))
        self.replies[-1].channel, self.replies[-1].guild = self.channel, self.guild
        return self.replies[-1]

    async def _fetch_message(self, message_id):
        for message in [self, *self.replies]:
            if message.id == message_id and not message.deleted:
                return message
        raise NOT_FOUND

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def _history(self, after, limit):
        other = SimpleNamespace(author=SimpleNamespace(id=REQUESTER), reference=None)
        for later in [other, *(r for r in self.replies if not r.deleted)]:
            yield later


@pytest.fixture
async def bot(tmp_path, monkeypatch):
    config = Config(
        discord_token="x",
        hardcover_token="y",
        usage_db=str(tmp_path / "usage.db"),
        usage_salt="salt",
    )
    bot = BookBot(config)
    bot._connection.user = SimpleNamespace(id=BOT)
    bot.lookups = []

    async def find_book(client, query):
        bot.lookups.append(query)
        return RESULTS[query.casefold()]

    async def links_for(book):
        return Links(hardcover_book_url(book.id), "https://g", "https://a")

    async def report_error(where, error):
        raise error

    monkeypatch.setattr(botmod, "find_book", find_book)
    bot.hardcover, bot.links_for, bot.report_error = None, links_for, report_error
    yield bot
    await settled(bot)  # a write still going when the log is closed crashes Python
    for task in bot._background_tasks:
        task.cancel()
    bot.usage.close()


async def edit(bot, message, content):
    message.content, message.edited_at = content, discord.utils.utcnow()
    await bot.on_raw_message_edit(SimpleNamespace(message=message))


def restart(bot):
    """Everything held in memory is lost; Discord and the usage log still have theirs."""
    bot.answered.clear()
    bot.replies.clear()
    bot.lookups.clear()


def logged(bot):
    found = bot.usage._db.execute("SELECT query, book_id FROM mentions ORDER BY id")
    return [tuple(row) for row in found]


async def test_editing_a_query_in_gets_a_reply(bot):
    message = FakeMessage("has anyone read dune")
    await bot.on_message(message)
    assert message.replies == []

    await edit(bot, message, "has anyone read {dune}")
    [reply] = message.replies
    assert reply.titles() == ["Dune"]
    assert logged(bot) == [("dune", 1)]


async def test_changing_a_query_replaces_the_reply_in_place(bot):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    assert "Couldn't find" in reply.shown["content"] and "view" not in reply.shown

    await edit(bot, message, "{circe}")
    assert message.replies == [reply] and reply.edits == 1
    assert reply.titles() == ["Circe"]
    assert reply.shown["content"] is None  # the "couldn't find" line is cleared
    assert [type(item).__name__ for item in reply.shown["view"].children] == ["DeleteButton"]

    await edit(bot, message, "{nothing}")
    assert reply.shown["embeds"] == [] and reply.shown["view"] is None
    assert "Couldn't find" in reply.shown["content"]
    assert logged(bot) == [("nothing", None)]


async def test_an_edit_that_leaves_the_queries_alone_does_nothing(bot):
    message = FakeMessage("{dune} is good")
    await bot.on_message(message)
    await edit(bot, message, "{Dune} is great")
    # A link preview loading or a pin: Discord reports an edit, but nothing changed.
    await bot.on_raw_message_edit(SimpleNamespace(message=message))
    [reply] = message.replies
    assert reply.edits == 0 and bot.lookups == ["dune"]
    assert len(logged(bot)) == 1


async def test_removing_every_query_deletes_the_reply(bot):
    message = FakeMessage("{dune} and {circe}")
    await bot.on_message(message)
    [reply] = message.replies
    await edit(bot, message, "never mind")
    assert reply.deleted
    assert bot.answered == {} and bot.replies == {} and logged(bot) == []


async def test_only_the_changed_queries_are_looked_up_and_logged(bot):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    await edit(bot, message, "{circe} and {dune}")
    assert bot.lookups == ["dune", "nothing", "circe"]
    assert logged(bot) == [("dune", 1), ("circe", 3)]
    [reply] = message.replies
    assert reply.titles() == ["Circe", "Dune"]
    assert [str(embed.color) for embed in reply.shown["embeds"]] == CARD_COLORS[:2]


async def test_a_picked_book_survives_an_edit(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    menu = picker_view(reply.id, bot.replies[reply.id]).children[0]
    menu.item._values = [str(DUNE_MESSIAH.id)]

    shown = {}

    async def defer():
        pass

    async def edit_original_response(**kwargs):
        shown.update(kwargs)

    clicked = SimpleNamespace(
        client=bot,
        response=SimpleNamespace(defer=defer),
        edit_original_response=edit_original_response,
    )
    await menu.callback(clicked)
    assert reply.titles() == ["Dune Messiah"]

    await edit(bot, message, "{circe} then {dune}")
    assert reply.titles() == ["Circe", "Dune Messiah"]
    assert bot.replies[reply.id].selected == {1: DUNE_MESSIAH.id}
    rows = bot.usage._db.execute("SELECT query, book_id, wrong_book FROM mentions ORDER BY id")
    assert [tuple(row) for row in rows] == [("dune", 2, 1), ("circe", 3, 0)]

    # The menu drawn before the edit now points at Circe's card, so it asks again.
    await menu.callback(clicked)
    assert "changed" in shown["content"]
    assert shown["view"].children[0].item.custom_id == f"bookbot:pick:{reply.id}:1"
    assert reply.titles() == ["Circe", "Dune Messiah"]


async def test_wrong_book_goes_when_the_edited_reply_has_no_other_matches(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    assert reply.id in bot.replies
    await edit(bot, message, "{circe}")
    assert reply.id not in bot.replies
    assert [type(item).__name__ for item in reply.shown["view"].children] == ["DeleteButton"]


async def test_an_edit_during_the_first_lookup_waits_and_gives_one_reply(bot, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    find_book = botmod.find_book

    async def slow_find_book(client, query):
        started.set()
        await release.wait()
        return await find_book(client, query)

    monkeypatch.setattr(botmod, "find_book", slow_find_book)
    message = FakeMessage("{nothing}")
    first = asyncio.create_task(bot.on_message(message))
    await started.wait()
    edited = FakeMessage("{dune}")
    edited.reply, edited.edited_at = message.reply, discord.utils.utcnow()
    second = asyncio.create_task(bot.on_raw_message_edit(SimpleNamespace(message=edited)))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    [reply] = message.replies
    assert reply.titles() == ["Dune"] and reply.edits == 1
    assert bot._locks == {}


async def test_after_a_restart_the_old_reply_is_found_and_edited(bot):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    restart(bot)

    await edit(bot, message, "{dune} and {circe}")
    [reply] = message.replies
    assert reply.edits == 1 and reply.titles() == ["Dune", "Circe"]
    assert reply.shown["content"] is None
    # Dune had to be looked up again, but is not counted twice.
    assert bot.lookups == ["dune", "circe"]
    assert logged(bot) == [("dune", 1), ("circe", 3)]


async def test_a_forgotten_message_is_left_alone_unless_its_text_just_changed(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    restart(bot)
    await bot.on_raw_message_edit(SimpleNamespace(message=message))  # pinned, say
    message.edited_at = discord.utils.utcnow() - botmod.RECENT_EDIT * 2
    await bot.on_raw_message_edit(SimpleNamespace(message=message))
    [reply] = message.replies
    assert reply.edits == 0 and bot.lookups == []


async def test_an_edit_after_the_reply_was_deleted_gets_a_new_reply(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    await message.replies[0].delete()
    await edit(bot, message, "{circe}")
    old, new = message.replies
    assert new.titles() == ["Circe"] and bot.answered[message.id].message is new
    await edit(bot, message, "nothing now")
    assert new.deleted


async def test_only_so_many_messages_are_remembered(bot, monkeypatch):
    monkeypatch.setattr(botmod, "REMEMBERED_MESSAGES", 2)
    for message_id in (1, 2, 3):
        await bot.on_message(FakeMessage("{circe}", id=message_id))
    assert list(bot.answered) == [2, 3]


async def test_a_failed_edit_is_flagged_on_the_message(bot):
    message = FakeMessage("{circe}")
    await bot.on_message(message)
    reported = []

    async def report_error(where, error):
        reported.append(where)

    bot.report_error = report_error
    await edit(bot, message, "{no such key}")
    assert message.reactions == [bot.error_reaction]
    assert reported == [f"an edit to this message: {message.jump_url}"]


# --- "Wrong book?" on a reply whose matches have been forgotten ----------------------------


def clicked(bot, reply, calls):
    return SimpleNamespace(
        client=bot,
        user=SimpleNamespace(id=REQUESTER),
        message=reply,
        channel=reply.channel,
        response=Recorder(calls),
        followup=Recorder(calls),
        edit_original_response=Recorder(calls).edit_original_response,
    )


async def wrong_book(bot, reply):
    """Click "Wrong book?" on a reply; returns what the requester was sent."""
    calls = []
    await WrongBookButton(REQUESTER).callback(clicked(bot, reply, calls))
    return calls


async def pick(bot, reply, book, embed_index=0):
    menu = dict(
        (item.embed_index, item) for item in picker_view(reply.id, bot.replies[reply.id]).children
    )[embed_index]
    await pick_from(bot, reply, menu, book)


async def pick_from(bot, reply, menu, book):
    menu.item._values = [str(book.id)]
    calls = []
    await menu.callback(clicked(bot, reply, calls))
    return calls


def offered(calls):
    """The books in the first menu of the picker last sent, and whether each is chosen."""
    options = calls[-1][1]["view"].children[0].item.options
    return [(option.label, option.default) for option in options]


async def test_wrong_book_after_a_restart_searches_again(bot):
    message = FakeMessage("{dune} and {circe}")
    await bot.on_message(message)
    [reply] = message.replies
    restart(bot)

    calls = await wrong_book(bot, reply)
    assert [name for name, _ in calls] == ["defer", "send"]
    assert calls[0][1] == {"ephemeral": True, "thinking": True}
    assert calls[1][1]["ephemeral"] is True
    assert offered(calls) == [
        ("Dune — Frank Herbert", True),
        ("Dune Messiah — Frank Herbert", False),
    ]
    assert bot.lookups == ["dune", "circe"]
    assert reply.edits == 0  # the reply itself is untouched until something is picked
    assert logged(bot) == [("dune", 1), ("circe", 3)]  # and nothing is counted twice

    await pick(bot, reply, DUNE_MESSIAH)
    assert reply.titles() == ["Dune Messiah", "Circe"]
    rows = bot.usage._db.execute("SELECT query, book_id, wrong_book FROM mentions ORDER BY id")
    assert [tuple(row) for row in rows] == [("dune", 2, 1), ("circe", 3, 0)]


async def test_a_picked_book_survives_a_restart(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    await pick(bot, reply, DUNE_MESSIAH)
    restart(bot)

    calls = await wrong_book(bot, reply)
    assert reply.titles() == ["Dune Messiah"] and reply.edits == 1
    assert offered(calls) == [
        ("Dune — Frank Herbert", False),
        ("Dune Messiah — Frank Herbert", True),
    ]
    await pick(bot, reply, DUNE)
    assert reply.titles() == ["Dune"]


async def test_once_the_matches_are_forgotten_the_cards_still_remembered_are_used(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    bot.replies.clear()  # as an hour later
    bot.lookups.clear()

    await wrong_book(bot, reply)
    assert bot.lookups == []
    await pick(bot, reply, DUNE_MESSIAH)
    # The pick is on the card an edit goes by, so it survives one.
    await edit(bot, message, "{circe} then {dune}")
    assert reply.titles() == ["Circe", "Dune Messiah"]


async def test_a_menu_opened_before_the_matches_were_forgotten_still_works(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    [menu] = picker_view(reply.id, bot.replies[reply.id]).children
    restart(bot)

    calls = await pick_from(bot, reply, menu, DUNE_MESSIAH)
    assert reply.titles() == ["Dune Messiah"]
    assert [name for name, _ in calls] == ["defer", "edit_original_response"]


async def test_wrong_book_goes_when_the_message_was_deleted(bot):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    restart(bot)
    message.deleted = True

    calls = await wrong_book(bot, reply)
    assert [type(item).__name__ for item in reply.shown["view"].children] == ["DeleteButton"]
    assert calls[-1] == ("send", {"args": (EXPIRED,), "ephemeral": True})
    assert bot.lookups == [] and reply.id not in bot.replies


@pytest.mark.parametrize("now", ["{dune} and {nothing}", "{dune}", "{dune} {circe} {dune messiah}"])
async def test_wrong_book_goes_when_the_cards_no_longer_fit_the_message(bot, monkeypatch, now):
    monkeypatch.setitem(RESULTS, "dune messiah", LookupResult(DUNE_MESSIAH, [DUNE_MESSIAH]))
    message = FakeMessage("{dune} and {circe}")
    await bot.on_message(message)
    [reply] = message.replies
    restart(bot)
    message.content = now  # edited while the bot was down, so the reply never followed

    calls = await wrong_book(bot, reply)
    assert [type(item).__name__ for item in reply.shown["view"].children] == ["DeleteButton"]
    assert calls[-1] == ("send", {"args": (EXPIRED,), "ephemeral": True})
    assert reply.titles() == ["Dune", "Circe"]


async def test_a_book_the_search_no_longer_returns_stays_and_none_is_marked_chosen(
    bot, monkeypatch
):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    restart(bot)
    monkeypatch.setitem(RESULTS, "dune", LookupResult(DUNE_MESSIAH, [DUNE_MESSIAH]))

    calls = await wrong_book(bot, reply)
    assert reply.titles() == ["Dune"] and reply.edits == 0
    assert offered(calls) == [("Dune Messiah — Frank Herbert", False)]
    await pick(bot, reply, DUNE_MESSIAH)
    assert reply.titles() == ["Dune Messiah"]


async def test_a_failed_search_leaves_wrong_book_for_another_try(bot, monkeypatch):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    restart(bot)
    reported = []

    async def report_error(where, error):
        reported.append(where)

    async def find_book(client, query):
        raise RuntimeError("Hardcover is down")

    bot.report_error = report_error
    monkeypatch.setattr(botmod, "find_book", find_book)
    calls = await wrong_book(bot, reply)
    assert reply.edits == 0 and reported == ["the 'Wrong book?' button"]
    assert "Try again later" in calls[-1][1]["args"][0]


# --- /book --------------------------------------------------------------------------------


def command(calls):
    sent = SimpleNamespace(id=2000, embeds=[])

    async def original_response():
        calls.append(("original_response", {}))
        return sent

    async def send(**kwargs):
        calls.append(("send", kwargs))
        return sent

    return SimpleNamespace(
        user=SimpleNamespace(id=REQUESTER),
        guild=None,
        channel=None,
        channel_id=30,
        response=Recorder(calls),
        followup=SimpleNamespace(send=send),
        original_response=original_response,
    )


def server_command(place, user_id=REQUESTER, sees_channel=True):
    """/book in a server that has added the bot. The replies go into `place.replies`,
    `place` being a FakeMessage standing in for the channel."""

    async def send_message(**kwargs):
        reply = FakeReply(None, kwargs)
        reply.reference, reply.interaction_metadata = None, SimpleNamespace()
        reply.channel, reply.guild = place.channel, place.guild
        place.replies.append(reply)

    async def original_response():
        return place.replies[-1]

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=place.guild,
        channel=place.channel,
        channel_id=place.channel.id,
        is_guild_integration=lambda: True,
        app_permissions=SimpleNamespace(view_channel=sees_channel),
        response=SimpleNamespace(send_message=send_message),
        original_response=original_response,
    )


async def settled(bot):
    """Let the bot finish logging which message answered a /book."""
    await asyncio.gather(
        *(t for t in bot._background_tasks if t.get_coro().__name__ == "record_reply")
    )


async def test_a_quick_book_command_is_answered_with_the_card_itself(bot):
    calls = []
    await bot.handle_command(command(calls), "dune")
    assert [name for name, _ in calls] == ["send_message", "original_response"]
    assert len(calls[0][1]["embeds"]) == 1
    assert bot.replies[2000].message.id == 2000  # "Wrong book?" edits the message sent


async def test_a_slow_book_command_says_it_is_thinking_first(bot, monkeypatch):
    async def find_book(client, query):
        await asyncio.sleep(0.01)
        return RESULTS[query]

    monkeypatch.setattr(botmod, "find_book", find_book)
    monkeypatch.setattr(botmod, "DIRECT_ANSWER_WITHIN", 0)
    calls = []
    await bot.handle_command(command(calls), "dune")
    assert [name for name, _ in calls] == ["defer", "send"]
    assert len(calls[1][1]["embeds"]) == 1 and calls[1][1]["wait"] is True


# --- Telling the owner what Hardcover lacks -----------------------------------------------


@pytest.fixture
def owner_dms(bot):
    dms = []

    async def tell_owner(text):
        dms.append(text)

    bot._owner_id, bot.tell_owner = 1, tell_owner
    return dms


async def reported(bot):
    """Let the background DMs to the owner finish."""
    await asyncio.gather(
        *(t for t in bot._background_tasks if t.get_coro().__name__ == "report_gaps")
    )


async def test_the_owner_hears_about_missing_books_and_missing_covers(bot, owner_dms):
    message = FakeMessage("{nothing}, {circe} and {piranesi}")
    await bot.on_message(message)
    await reported(bot)
    missing, gap = owner_dms
    assert "Couldn't find **nothing**" in missing
    assert f"asked for in #general in **RfM**: {message.jump_url}" in missing
    assert "hardcover.app/search?q=nothing" in missing
    assert "**Circe** has no cover and blurb" in gap and message.jump_url in gap
    assert hardcover_book_url(CIRCE.id) in gap
    assert "/retry-missed" in missing and "/retry-missed" in gap


async def test_the_owner_hears_about_each_gap_once_a_day(bot, owner_dms, monkeypatch):
    await bot.on_message(FakeMessage("{nothing} and {dune}"))
    await bot.on_message(FakeMessage("{Nothing} and {dune}", id=501))
    await reported(bot)
    assert len(owner_dms) == 2

    real = botmod.time.monotonic
    monkeypatch.setattr(botmod.time, "monotonic", lambda: real() + botmod.GAP_DM_INTERVAL)
    await bot.on_message(FakeMessage("{dune}", id=502))
    await reported(bot)
    assert len(owner_dms) == 3


async def test_an_edit_only_reports_the_queries_it_adds(bot, owner_dms):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    await edit(bot, message, "{nothing} and {circe}")
    await reported(bot)
    assert ["Circe" in dm for dm in owner_dms] == [False, True]


async def test_a_book_command_in_a_dm_is_reported_too(bot, owner_dms):
    await bot.handle_command(command([]), "nothing")
    await reported(bot)
    [dm] = owner_dms
    assert "Couldn't find **nothing**" in dm and "/book in a DM" in dm
    assert "`/retry-missed` can't update this reply" in dm  # nothing is logged from DMs


async def test_a_book_command_in_a_server_names_the_channel(bot, owner_dms):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "nothing")
    await bot.handle_command(server_command(place), "circe")
    await reported(bot)
    missing, gap = owner_dms
    # By name: a channel mention shows as "#unknown" to an owner not in the server.
    assert "/book in #general in **RfM**" in missing and "<#" not in missing
    # A found book can't be put into a /book reply, but a cover can be.
    assert "`/retry-missed` can't update this reply" in missing
    assert "Then `/retry-missed` updates the replies that showed it." in gap


async def test_a_braces_message_in_a_dm_is_reported_too(bot, owner_dms):
    message = FakeMessage("{circe}")
    message.guild = None
    await bot.on_message(message)
    await reported(bot)
    [dm] = owner_dms
    assert "**Circe**" in dm and message.jump_url in dm


# --- The tip about {…} on /book -----------------------------------------------------------


def tipped(reply):
    return botmod.BOOK_TIP in (reply.shown.get("content") or "")


async def test_each_person_gets_the_tip_once(bot):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "dune")
    await bot.handle_command(server_command(place), "circe")
    await bot.handle_command(server_command(place, user_id=43), "dune")
    await settled(bot)
    assert [tipped(reply) for reply in place.replies] == [True, False, True]
    assert "{Book Title by Author}" in place.replies[0].shown["content"]


async def test_a_reply_that_found_nothing_gets_the_tip_under_it(bot):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "nothing")
    await settled(bot)
    content = place.replies[0].shown["content"]
    assert content.startswith("Couldn't find") and content.endswith(botmod.BOOK_TIP)


async def test_no_tip_once_the_server_has_used_braces(bot):
    await bot.on_message(FakeMessage("{dune}"))
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "circe")
    await settled(bot)
    assert not tipped(place.replies[0])
    assert bot._braces_servers == {10}  # so the log isn't asked again


async def test_no_tip_where_braces_would_not_work(bot):
    calls = []
    await bot.handle_command(command(calls), "dune")  # a DM
    assert "content" not in calls[0][1]

    place = FakeMessage("")
    await bot.handle_command(server_command(place, sees_channel=False), "dune")
    # Not used up there: the person gets it in a channel the bot can see.
    await bot.handle_command(server_command(place), "circe")
    await settled(bot)
    assert [tipped(reply) for reply in place.replies] == [False, True]


async def test_the_tip_stays_when_the_book_is_switched(bot):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "dune")
    await settled(bot)
    [reply] = place.replies
    await pick(bot, reply, DUNE_MESSIAH)
    assert reply.titles() == ["Dune Messiah"] and tipped(reply)


async def test_a_broken_log_costs_only_the_tip(bot):
    reported = []

    async def report_error(where, error):
        reported.append(where)

    async def claim_tip(guild_id, user_id):
        raise RuntimeError("disk full")

    bot.report_error, bot.usage.claim_tip = report_error, claim_tip
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "dune")
    [reply] = place.replies
    assert reply.titles() == ["Dune"] and not tipped(reply)
    assert reported == ["checking whether to show the {…} tip"]
