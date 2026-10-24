"""/retry-missed: replies that couldn't find a book get it once Hardcover has it."""

import sqlite3
from types import SimpleNamespace

import discord
import pytest

from bookbot.hardcover import Book, HardcoverError
from bookbot.links import Links, hardcover_book_url
from bookbot.lookup import LookupResult
from bookbot.usage import Origin, UsageLog
from tests import test_edits
from tests.test_bot import Recorder
from tests.test_edits import (
    CIRCE,
    DUNE,
    DUNE_MESSIAH,
    NOT_FOUND,
    RESULTS,
    FakeMessage,
    logged,
    pick,
    restart,
    server_command,
    settled,
    wrong_book,
)

bot = test_edits.bot

OWNER = 7
PIRANESI = Book(id=4, title="Piranesi")
COVER = "https://assets.hardcover.app/cover.jpg"
# None of these has a cover or blurb, as Hardcover first had them.
BOOKS = {book.id: book for book in [DUNE, DUNE_MESSIAH, CIRCE, PIRANESI]}


@pytest.fixture
def added(monkeypatch):
    """Put a book on Hardcover that searches for "nothing" didn't find before."""

    def add():
        monkeypatch.setitem(RESULTS, "nothing", LookupResult(PIRANESI, [PIRANESI]))

    return add


# Books as Hardcover has them now, when that differs from the search results in RESULTS.
NOW: dict[int, Book] = {}


@pytest.fixture
def filled_in(monkeypatch):
    """Give a book a cover and blurb on Hardcover."""

    def fill(book):
        now = Book(id=book.id, title=book.title, description="Blurb.", cover_url=COVER)
        monkeypatch.setitem(NOW, book.id, now)

    return fill


async def retry(bot, *messages, user_id=OWNER, hours=24, covers_and_blurbs=None, dry_run=False):
    """Run /retry-missed; returns the last thing the person who ran it was told."""
    bot._owner_id = OWNER
    bot.lookups.clear()
    bot.fetched = []

    async def fake_covers_and_blurbs(book_ids):
        if covers_and_blurbs:
            return await covers_and_blurbs(book_ids)
        bot.fetched.append(book_ids)
        return {id: NOW.get(id, BOOKS[id]) for id in book_ids}

    bot.hardcover = SimpleNamespace(covers_and_blurbs=fake_covers_and_blurbs)
    channels = {message.channel.id: message.channel for message in messages}
    bot.get_channel = channels.get
    calls = []
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=Recorder(calls),
        edit_original_response=Recorder(calls).edit_original_response,
    )
    await bot.handle_retry(interaction, hours, dry_run)
    told = calls[-1][1]
    return told.get("content") or told["args"][0]


async def test_a_missed_book_replaces_the_not_found_line(bot, added):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    assert "Couldn't find" in reply.shown["content"]
    added()

    told = await retry(bot, message)
    assert message.replies == [reply] and reply.edits == 1
    assert reply.titles() == ["Dune", "Piranesi"] and reply.shown["content"] is None
    assert bot.lookups == ["nothing"]  # Dune's card is kept, not searched for again
    assert "1 query that found nothing, in 1 reply, and 1 book without a cover or blurb" in told
    assert "Updated 1 reply" in told and reply.jump_url in told
    rows = bot.usage._db.execute("SELECT query, book_id, wrong_book FROM mentions ORDER BY id")
    assert [tuple(row) for row in rows] == [("dune", 1, 0), ("nothing", 4, 0)]

    told = await retry(bot, message)
    assert told.startswith("Looked again for 2 books without a cover or blurb, in 1 reply, from")
    assert "found nothing" not in told


async def test_a_book_that_is_still_missing_costs_one_search_and_nothing_else(bot):
    first, second = FakeMessage("{nothing}", id=500), FakeMessage("{Nothing}", id=501)
    await bot.on_message(first)
    await bot.on_message(second)

    async def fetch_message(message_id):
        raise AssertionError("Discord should not be asked for the message")

    first.channel.fetch_message = second.channel.fetch_message = fetch_message
    told = await retry(bot, first, second)
    assert bot.lookups == ["nothing"]  # once, though two messages asked for it
    assert first.replies[0].edits == 0 and second.replies[0].edits == 0
    assert "2 queries that found nothing, in 2 replies, from the last 24 hours" in told
    assert "Still not found (2):** Nothing, nothing" in told and "Updated" not in told


async def test_after_a_restart_the_reply_is_found_and_a_picked_book_is_kept(bot, added):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    await pick(bot, reply, DUNE_MESSIAH)
    restart(bot)
    added()

    await retry(bot, message)
    assert message.replies == [reply]
    assert reply.titles() == ["Dune Messiah", "Piranesi"]
    assert logged(bot) == [("dune", 2), ("nothing", 4)]
    # "Wrong book?" still offers Dune's other match, with the picked one marked.
    calls = await wrong_book(bot, reply)
    assert test_edits.offered(calls) == [
        ("Dune — Frank Herbert", False),
        ("Dune Messiah — Frank Herbert", True),
    ]


@pytest.mark.parametrize("gone", ["message", "reply"])
@pytest.mark.parametrize("restarted", [False, True])
async def test_nothing_is_posted_when_the_message_or_the_reply_is_gone(bot, added, gone, restarted):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    (message if gone == "message" else reply).deleted = True
    if restarted:
        restart(bot)
    added()

    told = await retry(bot, message)
    assert message.replies == [reply] and reply.edits == 0
    assert "Left 1 reply alone" in told and "Updated" not in told
    assert logged(bot) == [("nothing", None)]


async def test_a_query_edited_out_while_the_bot_was_down_is_not_put_back(bot, added):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    restart(bot)
    message.content = "never mind"
    added()
    await retry(bot, message)
    assert message.replies[0].edits == 0


async def test_only_misses_from_the_hours_asked_for_are_retried(bot, added):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    bot.usage._db.execute("UPDATE mentions SET mentioned_at = '2020-01-01T00:00:00+00:00'")
    added()
    assert "Nothing to retry" in await retry(bot, message)
    assert bot.lookups == []


async def test_only_the_owner_can_retry(bot, added):
    message = FakeMessage("{nothing}")
    await bot.on_message(message)
    added()
    told = await retry(bot, message, user_id=OWNER + 1)
    assert "owner" in told and bot.lookups == [] and message.replies[0].edits == 0


async def test_one_failure_does_not_stop_the_other_replies(bot, added):
    broken, fine = FakeMessage("{nothing}", id=500), FakeMessage("{nothing}", id=501)
    await bot.on_message(broken)
    await bot.on_message(fine)
    restart(bot)
    reported = []

    async def report_error(where, error):
        reported.append(where)

    async def fetch_message(message_id):
        raise RuntimeError("boom")

    bot.report_error, broken.channel.fetch_message = report_error, fetch_message
    broken.channel.id = 21
    bot.usage._db.execute("UPDATE mentions SET channel_id = 21 WHERE message_id = 500")
    added()
    told = await retry(bot, broken, fine)
    assert reported == ["retrying a missed query"]
    assert fine.replies[0].titles() == ["Piranesi"]
    assert "Updated 1 reply" in told and "Left 1 reply alone" in told


async def test_a_message_in_a_thread_is_fetched_from_the_thread(bot, added):
    message = FakeMessage("{nothing}")
    thread = message.channel
    thread.id, thread.parent = 30, SimpleNamespace(id=20, name="general", category=None)
    await bot.on_message(message)
    restart(bot)
    added()
    await retry(bot, message)
    assert message.replies[0].titles() == ["Piranesi"]
    row = bot.usage._db.execute("SELECT channel_id, thread_id FROM mentions").fetchone()
    assert tuple(row) == (20, 30)


async def test_an_old_record_of_a_thread_message_is_looked_for_in_the_open_threads(bot, added):
    message = FakeMessage("{nothing}")
    thread = message.channel
    thread.id, thread.parent = 30, SimpleNamespace(id=20, name="general", category=None)
    await bot.on_message(message)
    bot.usage._db.execute("UPDATE mentions SET thread_id = NULL")  # as before it was kept
    restart(bot)
    added()

    async def not_here(message_id):
        raise NOT_FOUND

    later = SimpleNamespace(id=message.id + 1, fetch_message=None)  # opened after the message
    parent = SimpleNamespace(id=20, fetch_message=not_here, threads=[later, thread])
    await retry(bot, SimpleNamespace(channel=parent))
    assert message.replies[0].titles() == ["Piranesi"]


async def test_a_log_from_before_threads_were_kept_gains_the_column(tmp_path):
    path = str(tmp_path / "usage.db")
    UsageLog(path, "salt").close()
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE mentions DROP COLUMN thread_id")
    db.commit()
    db.close()

    usage = UsageLog(path, "salt")
    origin = Origin("message", 42, 10, "RfM", 20, "general", None, message_id=99, thread_id=30)
    await usage.record(origin, [("nope", None), ("dune", test_edits.DUNE)], None)
    [miss] = await usage.misses(1)
    assert (miss.query, miss.message_id, miss.channel_id, miss.thread_id) == ("nope", 99, 20, 30)
    usage.close()


@pytest.fixture
def goodreads_pages(bot):
    """Give every book a Goodreads page, so a card lacking a cover links its title there."""

    async def links_for(book):
        return Links(
            hardcover_book_url(book.id),
            f"https://www.goodreads.com/book/show/{book.id}",
            "https://a",
        )

    bot.links_for = links_for


async def test_a_card_gets_its_cover_once_hardcover_has_it(bot, filled_in, goodreads_pages):
    message = FakeMessage("{dune} and {circe}")
    await bot.on_message(message)
    [reply] = message.replies
    assert [embed.url for embed in reply.embeds] == [
        "https://www.goodreads.com/book/show/1",
        "https://www.goodreads.com/book/show/3",
    ]
    filled_in(DUNE)

    told = await retry(bot, message)
    assert bot.fetched == [[1, 3]] and bot.lookups == []
    assert message.replies == [reply] and reply.edits == 1
    [dune, circe] = reply.embeds
    assert (dune.url, dune.thumbnail.url) == (hardcover_book_url(1), COVER)
    assert (circe.url, circe.thumbnail.url) == ("https://www.goodreads.com/book/show/3", None)
    assert "2 books without a cover or blurb, in 1 reply, from the last 24 hours" in told
    assert f"**Updated 1 reply:**\n- Dune (#general in **RfM**): {reply.jump_url}" in told
    assert "**Still without a cover or blurb (1):** Circe" in told
    # The card kept in memory has the cover too, for when a book is switched back to it.
    assert bot.answered[message.id].cards[0].book.cover_url == COVER

    await retry(bot, message)
    assert bot.fetched == [[3]] and reply.edits == 1  # Dune is done with


async def test_after_a_restart_a_picked_book_gets_its_cover(bot, filled_in, goodreads_pages):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    await pick(bot, reply, DUNE_MESSIAH)
    restart(bot)
    filled_in(DUNE_MESSIAH)

    told = await retry(bot, message)
    assert bot.fetched == [[2]]
    [card] = reply.embeds
    assert (card.url, card.thumbnail.url) == (hardcover_book_url(2), COVER)
    assert reply.titles() == ["Dune Messiah"] and "Updated 1 reply" in told


@pytest.mark.parametrize("restarted", [False, True])
async def test_a_card_whose_reply_is_gone_is_tried_again_next_time(bot, filled_in, restarted):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    [reply] = message.replies
    reply.deleted = True
    if restarted:
        restart(bot)
    filled_in(DUNE)

    assert "Left 1 reply alone" in await retry(bot, message)
    await retry(bot, message)
    assert bot.fetched == [[1]]


async def test_hardcover_failing_on_covers_does_not_stop_the_missed_books(bot, added):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    added()
    reported = []

    async def report_error(where, error):
        reported.append(where)

    async def covers_and_blurbs(book_ids):
        raise HardcoverError("HTTP 500")

    bot.report_error = report_error
    told = await retry(bot, message, covers_and_blurbs=covers_and_blurbs)
    assert reported == ["looking up covers and blurbs"]
    assert reply.titles() == ["Dune", "Piranesi"] and "Updated 1 reply" in told


async def test_a_reply_changed_both_ways_counts_once(bot, added, filled_in):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    [reply] = message.replies
    added()
    filled_in(DUNE)

    told = await retry(bot, message)
    assert f"**Updated 1 reply:**\n- Piranesi, Dune (#general in **RfM**): {reply.jump_url}" in told
    assert reply.titles() == ["Dune", "Piranesi"]
    assert reply.embeds[0].thumbnail.url == COVER


@pytest.mark.parametrize("restarted", [False, True])
async def test_a_book_command_card_gets_its_cover(bot, filled_in, goodreads_pages, restarted):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "dune")
    await settled(bot)
    [reply] = place.replies
    assert reply.embeds[0].url == "https://www.goodreads.com/book/show/1"
    if restarted:
        restart(bot)  # or "Wrong book?" has been put away; either way only Discord has it
    filled_in(DUNE)

    told = await retry(bot, place)
    assert bot.fetched == [[1]] and reply.edits == 1
    [card] = reply.embeds
    assert (card.url, card.thumbnail.url) == (hardcover_book_url(1), COVER)
    assert "1 book without a cover or blurb, in 1 reply" in told
    assert f"**Updated 1 reply:**\n- Dune (#general in **RfM**): {reply.jump_url}" in told
    if not restarted:
        # "Wrong book?" switching back to Dune would make its card again from this.
        assert bot.replies[reply.id].cards[0].book.cover_url == COVER

    await retry(bot, place)
    assert reply.edits == 1  # done with


async def test_a_book_command_from_before_replies_were_logged_is_found(
    bot, filled_in, goodreads_pages
):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "circe")
    await bot.handle_command(server_command(place), "dune")
    await settled(bot)
    circe, dune = place.replies
    bot.usage._db.execute("UPDATE mentions SET reply_id = NULL")
    restart(bot)
    filled_in(DUNE)

    told = await retry(bot, place)
    assert circe.edits == 0 and dune.edits == 1
    assert dune.embeds[0].thumbnail.url == COVER and "Updated 1 reply" in told


async def test_a_book_command_that_found_nothing_is_left_as_it_is(bot, added):
    place = FakeMessage("")
    await bot.handle_command(server_command(place), "nothing")
    await settled(bot)
    added()

    assert (await retry(bot, place)).startswith("Nothing to retry")


@pytest.mark.parametrize("restarted", [False, True])
async def test_a_dry_run_changes_nothing(bot, added, filled_in, goodreads_pages, restarted):
    message = FakeMessage("{dune} and {nothing}")
    await bot.on_message(message)
    place = FakeMessage("", id=600)
    place.channel.id = 21
    await bot.handle_command(server_command(place), "circe")
    await settled(bot)
    [reply], [command_reply] = message.replies, place.replies
    if restarted:
        restart(bot)
    added()
    filled_in(DUNE)
    filled_in(CIRCE)
    before = [embed.to_dict() for embed in [*reply.embeds, *command_reply.embeds]]

    told = await retry(bot, message, place, dry_run=True)
    assert told.startswith("Dry run: nothing was changed.\nLooked again for")
    assert "**Would update 2 replies:**" in told
    assert f"- Piranesi, Dune (#general in **RfM**): {reply.jump_url}" in told
    assert f"- Circe (#general in **RfM**): {command_reply.jump_url}" in told
    assert reply.edits == 0 and command_reply.edits == 0
    # Nor the cards kept in memory, which a later edit would send.
    after = [embed.to_dict() for embed in [*reply.embeds, *command_reply.embeds]]
    assert after == before
    assert logged(bot) == [("dune", 1), ("nothing", None), ("circe", 3)]

    told = await retry(bot, message, place)
    assert "**Updated 2 replies:**" in told
    assert reply.titles() == ["Dune", "Piranesi"]
    assert command_reply.embeds[0].thumbnail.url == COVER


async def test_a_reply_out_of_reach_says_what_permission_is_missing(bot, filled_in):
    message = FakeMessage("{dune}")
    await bot.on_message(message)
    restart(bot)
    filled_in(DUNE)

    async def fetch_message(message_id):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")

    message.channel.fetch_message = fetch_message
    message.channel.guild = SimpleNamespace(name="RfM", me=SimpleNamespace())
    message.channel.permissions_for = lambda member: SimpleNamespace(
        view_channel=True, read_message_history=False
    )
    told = await retry(bot, message, dry_run=True)
    assert "**Would leave 1 reply alone**" in told
    assert "- The bot lacks Read Message History in #general in **RfM**" in told
