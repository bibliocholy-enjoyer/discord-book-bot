"""/stats: the owner gets the usage report in a DM."""

from types import SimpleNamespace

from bookbot import stats
from bookbot.bot import stats_messages
from bookbot.stats import Report, Section
from bookbot.usage import Origin
from tests import test_edits
from tests.test_bot import Recorder
from tests.test_edits import CIRCE, FakeMessage
from tests.test_usage import HQ_ID, ORIGIN

bot = test_edits.bot

OWNER = 7


async def run_stats(bot, monkeypatch, user_id=OWNER, **options):
    """Run /stats; returns the messages only the person who ran it can see."""
    answers = {
        "/applications/@me": {"name": "BookBot", "approximate_user_install_count": 3},
        "/users/@me/guilds?with_counts=true": [
            {"id": "10", "name": "RfM", "approximate_member_count": 900},
        ],
    }
    monkeypatch.setattr(stats, "ask_discord", lambda path, token: answers[path])
    bot._owner_id = OWNER
    calls = []
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=Recorder(calls),
        followup=Recorder(calls),
        edit_original_response=Recorder(calls).edit_original_response,
    )
    await bot.handle_stats(interaction, options.pop("server", None), None, **options)
    said = [
        kwargs.get("content") or kwargs["args"][0]
        for name, kwargs in calls
        if name in ("send_message", "edit_original_response", "send")
    ]
    # Kept in the DM, so earlier reports can be looked back at; a refusal isn't.
    refused = calls[0][0] == "send_message"
    assert all(kwargs.get("ephemeral", False) == refused for _, kwargs in calls)
    assert all(len(text) <= 2000 for text in said)
    return "\n\n".join(said)


def books(told):
    return told.split("**Most mentioned books**")[1].split("**Servers**")[0]


async def test_the_owner_gets_the_report_as_messages(bot, monkeypatch):
    await bot.on_message(FakeMessage("{dune}"))
    told = await run_stats(bot, monkeypatch)
    # What's left out, in small print, first.
    assert told.startswith("-# Leaving out servers: BookBot test server, BookBot HQ\n")
    assert "-# Leaving out the owner" in told
    assert "**Where BookBot is installed**\nServers: 1. Personal accounts: about 3 (" in told
    assert "• RfM (900 members) `10`" in told
    assert "1 lookup by 1 person in 1 server, of 1 book. Not found: 0." in told
    assert "• **Dune** by Frank Herbert: 1 person, 1 mention, 1 server" in books(told)
    assert "**Not found (most asked first)**\nNone yet." in told


async def test_each_server_says_how_it_asks(bot, monkeypatch):
    await bot.on_message(FakeMessage("{dune} and {circe}"))
    command = Origin(**{**ORIGIN.__dict__, "source": "command"})
    await bot.usage.record(command, [("circe", CIRCE)], None)
    told = await run_stats(bot, monkeypatch)
    assert "• **RfM**: 3 lookups (2 {…}, 1 /book), 1 person, last " in told


async def test_the_owners_books_are_left_out_unless_asked_for(bot, monkeypatch):
    await bot.on_message(FakeMessage("{dune}"))
    owner = Origin(**{**ORIGIN.__dict__, "user_id": OWNER})
    await bot.usage.record(owner, [("circe", CIRCE)], None)
    told = await run_stats(bot, monkeypatch)
    assert "Circe" not in books(told) and "Dune" in books(told)
    told = await run_stats(bot, monkeypatch, owner="include")
    assert "Circe" in books(told) and "Dune" in books(told)
    told = await run_stats(bot, monkeypatch, owner="only")
    assert "-# Only the owner" in told
    assert "Circe" in books(told) and "Dune" not in books(told)


async def test_the_bots_own_servers_are_left_out_unless_asked_for(bot, monkeypatch):
    hq = Origin(**{**ORIGIN.__dict__, "guild_id": HQ_ID, "guild_name": "BookBot HQ"})
    await bot.usage.record(hq, [("circe", CIRCE)], None)
    assert "Circe" not in books(await run_stats(bot, monkeypatch))
    assert "Circe" in books(await run_stats(bot, monkeypatch, all_servers=True))
    told = await run_stats(bot, monkeypatch, server=" BookBot HQ , 12345")
    assert "-# Only servers: BookBot HQ, 12345" in told and "Leaving out servers" not in told
    assert "Circe" in books(told)


def test_a_long_report_is_split_between_messages():
    rows = [{"query": f"book number {n} " + "x" * 50, "times": 1} for n in range(100)]
    report = Report(
        ["Leaving out the owner"],
        [
            Section("overview", "Overview", [], [], "short"),
            Section("not_found", "Not found", ["query"], rows),
        ],
    )
    messages = stats_messages(report)
    assert len(messages) > 2 and all(len(m) <= 2000 for m in messages)
    assert messages[0].startswith("-# Leaving out the owner\n\n**Overview**\nshort")
    together = "\n".join(messages)
    assert all(f"• book number {n} " in together for n in range(100))
    # Names are shown as typed, not as Discord markdown.
    [line] = stats_messages(
        Report([], [Section("not_found", "x", [], [{"query": "*a*", "times": 2}])])
    )
    assert "\\*a\\*" in line


async def test_only_the_owner_can_see_the_stats(bot, monkeypatch):
    told = await run_stats(bot, monkeypatch, user_id=99)
    assert told == "Only the bot's owner can use this."
