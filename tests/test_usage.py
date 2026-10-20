"""The usage log: what gets stored, what doesn't, and that it can't break a reply."""

from types import SimpleNamespace

import pytest

import bookbot.bot as botmod
from bookbot import stats
from bookbot.bot import BookBot, command_origin, message_origin, picker_view
from bookbot.config import Config
from bookbot.hardcover import Author, Book
from bookbot.links import Links
from bookbot.lookup import LookupResult
from bookbot.sentiment import score_message
from bookbot.usage import Origin, UsageLog, connect, hash_user

DUNE = Book(
    id=1, title="Dune", authors=[Author(id=1, name="Frank Herbert")], genres=["Sci-Fi"], pages=412
)
DUNE_MESSIAH = Book(id=2, title="Dune Messiah", authors=[Author(id=1, name="Frank Herbert")])
DUNE_COPY = Book(id=3, title="Dune!")
HQ_ID, TEST_ID = 1561056660674318378, 1556923641466196023  # the bot's own servers
ORIGIN = Origin(
    source="message",
    user_id=42,
    guild_id=10,
    guild_name="RfM",
    channel_id=20,
    channel_name="currently-reading",
    category_name="Romance",
    message_id=99,
)


@pytest.fixture
def usage(tmp_path):
    log = UsageLog(str(tmp_path / "usage.db"), "salt")
    yield log
    log.close()


def rows(usage, sql):
    return [dict(r) for r in usage._db.execute(sql)]


async def test_records_a_mention_and_its_book(usage):
    ids = await usage.record(ORIGIN, [("dune", DUNE), ("nope", None)], ("positive", 0.6))
    assert len(ids) == 2
    [found, missing] = rows(usage, "SELECT * FROM mentions ORDER BY id")
    assert (found["guild_name"], found["channel_name"], found["category_name"]) == (
        "RfM",
        "currently-reading",
        "Romance",
    )
    assert (found["query"], found["book_id"], found["source"]) == ("dune", 1, "message")
    assert (found["sentiment_label"], found["message_id"], found["wrong_book"]) == (
        "positive",
        99,
        0,
    )
    assert (missing["query"], missing["book_id"]) == ("nope", None)
    [book] = rows(usage, "SELECT * FROM books")
    assert (book["title"], book["authors"], book["pages"]) == ("Dune", "Frank Herbert", 412)


async def test_stores_no_user_id_or_message_text(usage):
    await usage.record(ORIGIN, [("dune", DUNE)], ("neutral", 0.0))
    [row] = rows(usage, "SELECT * FROM mentions")
    assert row["user_hash"] == hash_user("salt", 42)
    assert "42" not in row["user_hash"] and row["user_hash"] != "42"
    assert set(row) & {"user_id", "content", "text"} == set()


def test_hash_is_stable_and_depends_on_the_salt():
    assert hash_user("a", 1) == hash_user("a", 1)
    assert hash_user("a", 1) != hash_user("a", 2)
    assert hash_user("a", 1) != hash_user("b", 1)


async def test_seeing_a_book_again_refreshes_it(usage):
    await usage.record(ORIGIN, [("dune", DUNE)], None)
    await usage.record(ORIGIN, [("dune", Book(id=1, title="Dune", users_count=500))], None)
    [book] = rows(usage, "SELECT * FROM books")
    assert book["users_count"] == 500


async def test_switching_book_corrects_the_mention(usage):
    [mention_id] = await usage.record(ORIGIN, [("dune", DUNE)], None)
    await usage.switch_book(mention_id, DUNE_MESSIAH)
    [row] = rows(usage, "SELECT * FROM mentions")
    assert (row["book_id"], row["wrong_book"]) == (2, 1)
    assert {b["id"] for b in rows(usage, "SELECT id FROM books")} == {1, 2}


def test_sentiment_ignores_the_titles_in_braces():
    assert score_message("I loved {The Hate U Give}")[0] == "positive"
    assert score_message("{The Love Hypothesis}")[0] == "neutral"
    assert score_message("{Dune} was awful, I hated it")[0] == "negative"


def test_config_needs_a_salt_to_record(monkeypatch):
    for name, value in {"DISCORD_TOKEN": "x", "HARDCOVER_TOKEN": "y", "USAGE_DB": "/d"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("USAGE_SALT", raising=False)
    with pytest.raises(SystemExit):
        Config.from_env()
    monkeypatch.setenv("USAGE_SALT", "s")
    assert Config.from_env().usage_db == "/d"


def test_origin_of_a_thread_is_its_parent_channel():
    category = SimpleNamespace(name="Interests")
    parent = SimpleNamespace(id=5, name="femdom", category=category, parent=None)
    thread = SimpleNamespace(id=6, name="a thread", category=category, parent=parent)
    guild = SimpleNamespace(id=1, name="RfM")
    message = SimpleNamespace(
        guild=guild, channel=thread, author=SimpleNamespace(id=7), id=8, content="{x}"
    )
    origin = message_origin(message)
    assert (origin.channel_id, origin.channel_name, origin.category_name) == (
        5,
        "femdom",
        "Interests",
    )
    assert (origin.thread_id, origin.thread_name) == (6, "a thread")
    origin = message_origin(SimpleNamespace(**{**message.__dict__, "channel": parent}))
    assert (origin.channel_name, origin.thread_id, origin.thread_name) == ("femdom", None, None)


def test_direct_messages_are_not_recorded():
    message = SimpleNamespace(guild=None, author=SimpleNamespace(id=7))
    assert message_origin(message) is None
    assert command_origin(SimpleNamespace(guild=None)) is None


def test_a_command_in_a_server_without_the_bot_is_not_recorded():
    channel = SimpleNamespace(id=5, name="general", category=None, parent=None)
    used = SimpleNamespace(
        guild=SimpleNamespace(id=1, name="RfM"), channel=channel, user=SimpleNamespace(id=7)
    )
    used.is_guild_integration = lambda: False  # run from the asker's own account
    assert command_origin(used) is None
    used.is_guild_integration = lambda: True
    assert command_origin(used).guild_id == 1


@pytest.fixture
async def bot(tmp_path, monkeypatch):
    config = Config(
        discord_token="x",
        hardcover_token="y",
        usage_db=str(tmp_path / "usage.db"),
        usage_salt="salt",
    )
    bot = BookBot(config)
    results = {
        "dune": LookupResult(book=DUNE, alternatives=[DUNE, DUNE_MESSIAH]),
        "nothing": LookupResult(book=None),
    }

    async def find_book(client, query):
        return results[query]

    async def links_for(book):
        return Links("https://h", "https://g", "https://a")

    monkeypatch.setattr(botmod, "find_book", find_book)
    bot.hardcover = None
    bot.links_for = links_for
    yield bot
    bot.usage.close()


async def test_a_lookup_is_logged_with_its_sentiment(bot):
    await bot._respond(["dune", "nothing"], 42, ORIGIN, "just finished {dune}, loved it")
    mentions = rows(bot.usage, "SELECT query, book_id, sentiment_label FROM mentions ORDER BY id")
    assert mentions == [
        {"query": "dune", "book_id": 1, "sentiment_label": "positive"},
        {"query": "nothing", "book_id": None, "sentiment_label": "positive"},
    ]


async def test_a_broken_log_does_not_break_the_reply(bot):
    bot.usage._db.close()
    kwargs, reply = await bot._respond(["dune"], 42, ORIGIN, "{dune}")
    assert len(kwargs["embeds"]) == 1 and reply.mention_ids == {}


async def test_switching_in_the_menu_updates_the_log(bot):
    kwargs, reply = await bot._respond(["dune"], 42, ORIGIN, "{dune}")
    calls = []

    class Recorder:
        def __getattr__(self, name):
            async def method(*args, **kw):
                calls.append(name)

            return method

    reply.message = SimpleNamespace(edit=Recorder().edit)
    bot.replies[1000] = reply
    item = picker_view(1000, reply).children[0]
    item.item._values = ["2"]
    interaction = SimpleNamespace(
        client=bot, response=Recorder(), edit_original_response=Recorder().edit_original_response
    )
    await item.callback(interaction)
    [row] = rows(bot.usage, "SELECT book_id, wrong_book FROM mentions")
    assert row == {"book_id": 2, "wrong_book": 1}


async def test_stats_report_counts_distinct_people_and_merges(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")
    other = Origin(**{**ORIGIN.__dict__, "user_id": 43})
    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(other, [("dune!", DUNE_COPY)], None)
    log.close()
    monkeypatch.setattr("sys.argv", ["stats", path])
    stats.main()
    first = capsys.readouterr().out
    assert "Dune!" in first  # listed as a possible duplicate of Dune
    monkeypatch.setattr("sys.argv", ["stats", "merge", "3", "1", path])
    stats.main()
    monkeypatch.setattr("sys.argv", ["stats", path])
    stats.main()
    merged = capsys.readouterr().out
    section = merged.split("== Most mentioned books ==")[1].split("==")[0]
    assert "Dune" in section and "3" in section and "2" in section  # 3 mentions, 2 people


async def test_stats_can_pick_or_leave_out_servers(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")
    test_server = Origin(**{**ORIGIN.__dict__, "guild_id": 11, "guild_name": "Test Server"})
    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(test_server, [("dune messiah", DUNE_MESSIAH)], None)
    log.close()

    def report(*flags):
        monkeypatch.setattr("sys.argv", ["stats", *flags, path])
        stats.main()
        out = capsys.readouterr().out
        return out.split("== Most mentioned books ==")[1].split("==")[0]

    both = report()
    assert "Dune Messiah" in both and "Frank Herbert" in both
    only_rfm = report("--server", "rfm")  # by name, in any case
    assert "Dune Messiah" not in only_rfm and "Dune" in only_rfm
    without_test = report("--exclude", "Test Server")
    assert "Dune Messiah" not in without_test and "Dune" in without_test
    by_id = report("--server", "11")
    assert "Dune Messiah" in by_id and "Frank Herbert" in by_id
    monkeypatch.setattr("sys.argv", ["stats", "--exclude", "11", "--exclude", "10", path])
    stats.main()
    assert "(none yet)" in capsys.readouterr().out.split("== Most mentioned books ==")[1]


async def test_stats_list_each_thread_under_its_channel(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")

    def post(thread_id, thread_name=None):
        details = {**ORIGIN.__dict__, "channel_id": 30, "channel_name": "interests"}
        return Origin(**{**details, "thread_id": thread_id, "thread_name": thread_name})

    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(post(31, "femdom"), [("dune", DUNE)], None)
    await log.record(post(31, "femdom"), [("dune", DUNE)], None)
    # Recorded before the log kept thread names: Discord is asked for these.
    await log.record(post(32), [("dune", DUNE)], None)
    await log.record(post(33), [("dune", DUNE)], None)
    log.close()

    def ask_discord(path, token):
        if path == "/channels/32":
            return {"name": "monster girls"}
        raise OSError("gone")

    def channels():
        monkeypatch.setattr("sys.argv", ["stats", path])
        stats.main()
        section = capsys.readouterr().out.split("== Channels ==")[1].split("==")[0]
        return [line.split()[2:] for line in section.strip().splitlines()[1:]]

    monkeypatch.setattr(stats, "ask_discord", ask_discord)
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    assert sorted(channels()) == [
        ["currently-reading", "1"],
        ["interests", "(name", "not", "known)", "1"],
        ["interests", "femdom", "2"],
        ["interests", "monster", "girls", "1"],
    ]
    # Without a token nobody can be asked, and the report still prints.
    monkeypatch.delenv("DISCORD_TOKEN")
    assert ["interests", "(name", "not", "known)", "1"] in channels()


def test_an_older_log_gains_the_thread_columns(tmp_path):
    path = str(tmp_path / "usage.db")
    UsageLog(path, "salt").close()
    db = connect(path)
    db.execute("ALTER TABLE mentions DROP COLUMN thread_name")
    db.execute("ALTER TABLE mentions DROP COLUMN thread_id")
    db.commit()
    db.close()
    log = UsageLog(path, "salt")
    columns = {row["name"] for row in log._db.execute("PRAGMA table_info(mentions)")}
    log.close()
    assert {"thread_id", "thread_name"} <= columns


async def test_cards_without_a_cover_or_blurb_are_kept_track_of(usage):
    covered = Book(id=4, title="Circe", description="Blurb.", cover_url="https://c/circe.jpg")
    ids = await usage.record(ORIGIN, [("dune", DUNE), ("circe", covered), ("nope", None)], None)
    [entry] = await usage.incomplete(1)
    assert (entry.mention_id, entry.book_id, entry.message_id) == (ids[0], 1, 99)

    await usage.switch_book(ids[0], covered)
    assert await usage.incomplete(1) == []
    await usage.switch_book(ids[0], DUNE)
    await usage.found(ids[2], DUNE_MESSIAH)
    assert [entry.mention_id for entry in await usage.incomplete(1)] == [ids[0], ids[2]]

    await usage.completed([ids[0]])
    assert [entry.mention_id for entry in await usage.incomplete(1)] == [ids[2]]


def test_an_older_log_gains_the_incomplete_column(tmp_path):
    path = str(tmp_path / "usage.db")
    UsageLog(path, "salt").close()
    db = connect(path)
    db.execute("ALTER TABLE mentions DROP COLUMN incomplete")
    db.execute(
        "INSERT INTO mentions (mentioned_at, guild_id, source, query, user_hash)"
        " VALUES ('2026-01-01', 10, 'message', 'dune', 'h')"
    )
    db.commit()
    db.close()
    log = UsageLog(path, "salt")
    [row] = rows(log, "SELECT incomplete FROM mentions")
    log.close()
    assert row == {"incomplete": 0}


async def test_stats_leave_out_the_owner_unless_asked(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")
    owner = Origin(**{**ORIGIN.__dict__, "user_id": 43})
    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(owner, [("dune messiah", DUNE_MESSIAH)], None)
    log.close()
    answers = {
        "/applications/@me": {
            "name": "BookBot",
            "owner": {"id": "43"},
            "approximate_user_install_count": 3,
        },
        "/users/@me/guilds?with_counts=true": [],
    }
    monkeypatch.setattr(stats, "ask_discord", lambda path, token: answers[path])
    monkeypatch.setenv("USAGE_SALT", "salt")
    monkeypatch.delenv("OWNER_ID", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    def report(*flags):
        monkeypatch.setattr("sys.argv", ["stats", *flags, path])
        stats.main()
        return capsys.readouterr().out

    def books(out):
        return out.split("== Most mentioned books ==")[1].split("==")[0]

    # The owner is OWNER_ID when that is set, with no need to ask Discord, and their
    # books are left out unless asked for.
    monkeypatch.setenv("OWNER_ID", "43")
    out = report("--server", "rfm")
    assert "Leaving out the owner" in out and "Dune Messiah" not in books(out)
    assert "Dune Messiah" in books(report("--include-owner"))
    out = report("--only-owner")
    assert "Only the owner" in out
    [_, only] = books(out).strip().splitlines()  # the header, and one book
    assert only.startswith("Dune Messiah")

    # Otherwise it is the application's owner, or the owner of its team.
    monkeypatch.delenv("OWNER_ID")
    out = report()
    assert "Not leaving out the owner: this needs OWNER_ID, or DISCORD_TOKEN" in out
    assert "Dune Messiah" in books(out)  # the report still goes on, with the owner
    with pytest.raises(SystemExit, match="OWNER_ID, or DISCORD_TOKEN"):
        report("--only-owner")
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    out = report()
    assert "Dune Messiah" not in books(out)
    # Discord's count of personal accounts can't have the owner taken out, so it says so.
    assert "Personal accounts: about 3 (the owner is not left out of this)." in out
    assert "about 3 (everyone, not just the owner)." in report("--only-owner")
    assert "Personal accounts: about 3." in report("--include-owner")
    answers["/applications/@me"]["team"] = {"owner_user_id": "42"}
    assert "Dune Messiah" in books(report())

    # Without the salt there is no telling which hash is the owner's.
    monkeypatch.delenv("USAGE_SALT")
    assert "Not leaving out the owner: this needs USAGE_SALT" in report()
    with pytest.raises(SystemExit, match="USAGE_SALT"):
        report("--only-owner")
    with pytest.raises(SystemExit):  # one or the other
        report("--only-owner", "--include-owner")


async def test_stats_leave_out_the_bots_own_servers(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")
    hq = Origin(**{**ORIGIN.__dict__, "guild_id": HQ_ID, "guild_name": "BookBot HQ"})
    # Matched by ID, so still left out after being renamed.
    test = Origin(**{**ORIGIN.__dict__, "guild_id": TEST_ID, "guild_name": "Test Lab"})
    # Not the bot's, though it has the same name as one of them.
    namesake = Origin(**{**ORIGIN.__dict__, "guild_id": 14, "guild_name": "BookBot HQ"})
    await log.record(ORIGIN, [("dune", DUNE)], None)
    await log.record(hq, [("dune messiah", DUNE_MESSIAH)], None)
    await log.record(test, [("dune!", DUNE_COPY)], None)
    await log.record(namesake, [("dune", DUNE)], None)
    log.close()

    def servers(*flags):
        monkeypatch.setattr("sys.argv", ["stats", "--include-owner", *flags, path])
        stats.main()
        out = capsys.readouterr().out
        return out.split("== Servers ==")[1].split("==")[0]

    default = servers()
    assert "RfM" in default and "Test Lab" not in default
    assert f"BookBot HQ  {HQ_ID}" not in default and "BookBot HQ  14" in default
    everything = servers("--all-servers")
    assert "RfM" in everything and "HQ" in everything and "Test" in everything
    # Asking for one of them by name, or by ID, shows it.
    only_hq = servers("--server", "bookbot hq")
    assert "HQ" in only_hq and "RfM" not in only_hq
    assert "Test Lab" in servers("--server", str(TEST_ID))
    # --exclude leaves out more, on top of the bot's own servers.
    assert "(none yet)" in servers("--exclude", "rfm", "--exclude", "14")
    assert "HQ" in servers("--all-servers", "--exclude", "rfm")


def reported(bot, monkeypatch):
    seen = []

    async def report_error(where, error):
        seen.append(where)

    monkeypatch.setattr(bot, "report_error", report_error)
    return seen


async def test_a_log_that_cannot_be_opened_does_not_stop_the_bot(tmp_path, monkeypatch):
    config = Config(
        discord_token="x",
        hardcover_token="y",
        usage_db=str(tmp_path / "missing folder" / "usage.db"),
        usage_salt="salt",
    )
    bot = BookBot(config)
    assert bot.usage is None
    seen = reported(bot, monkeypatch)

    async def find_book(client, query):
        return LookupResult(book=DUNE, alternatives=[DUNE])

    async def links_for(book):
        return Links("https://h", "https://g", "https://a")

    monkeypatch.setattr(botmod, "find_book", find_book)
    bot.hardcover, bot.links_for = None, links_for
    kwargs, _ = await bot._respond(["dune"], 42, ORIGIN, "{dune}")
    assert len(kwargs["embeds"]) == 1  # still answers
    assert seen == []
    await bot.report_usage_log_failure()  # what setup_hook does once connected
    assert seen == ["opening the usage log (nothing is being recorded)"]


async def test_no_report_when_the_log_opened_fine(bot, monkeypatch):
    seen = reported(bot, monkeypatch)
    await bot.report_usage_log_failure()
    assert seen == []


async def test_the_owner_hears_when_recording_fails(bot, monkeypatch):
    seen = reported(bot, monkeypatch)
    bot.usage._db.close()
    await bot._respond(["dune"], 42, ORIGIN, "{dune}")
    assert seen == ["recording usage"]


def test_stats_start_with_where_the_bot_is_installed(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    UsageLog(path, "salt").close()
    answers = {
        "/applications/@me": {"name": "BookBot", "approximate_user_install_count": 3},
        "/users/@me/guilds?with_counts=true": [
            {"id": "11", "name": "Small", "approximate_member_count": 5},
            {"id": "10", "name": "RfM", "approximate_member_count": 900},
        ],
    }
    monkeypatch.setattr(stats, "ask_discord", lambda path, token: answers[path])
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setattr("sys.argv", ["stats", path])
    stats.main()
    out = capsys.readouterr().out
    assert "== Where BookBot is installed ==" in out
    assert "Servers: 2. Personal accounts: about 3." in out
    assert out.index("RfM") < out.index("Small")  # biggest first

    # The server filters apply to the list of servers, but personal accounts aren't in
    # any server, so that count stays.
    for filters in (["--server", "rfm"], ["--exclude", "11"]):
        monkeypatch.setattr("sys.argv", ["stats", *filters, path])
        stats.main()
        out = capsys.readouterr().out
        assert "Servers: 1 of 2. Personal accounts: about 3." in out
        assert "RfM" in out and "Small" not in out

    # Filters that match no server leave a count and no table.
    monkeypatch.setattr("sys.argv", ["stats", "--server", "nowhere", path])
    stats.main()
    assert "Servers: 0 of 2. Personal accounts: about 3." in capsys.readouterr().out

    # Discord leaving the count out is not the same as a count of zero.
    del answers["/applications/@me"]["approximate_user_install_count"]
    stats.main()
    assert "Personal accounts: unknown (Discord didn't say)." in capsys.readouterr().out


def test_stats_still_print_when_discord_cannot_be_asked(tmp_path, capsys, monkeypatch):
    path = str(tmp_path / "usage.db")
    UsageLog(path, "salt").close()

    def ask_discord(path, token):
        raise OSError("no network")

    monkeypatch.setattr(stats, "ask_discord", ask_discord)
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setattr("sys.argv", ["stats", path])
    stats.main()
    out = capsys.readouterr().out
    assert "could not ask Discord: no network" in out and "== Overview ==" in out


async def test_the_owner_hears_when_the_bot_joins_or_leaves_a_server(bot):
    told = []

    async def send(text):
        told.append(text)

    bot._owner_id = 7
    bot.get_user = lambda user_id: SimpleNamespace(send=send)
    bot._connection._guilds = {1: object()}
    await bot.on_guild_join(SimpleNamespace(id=1, name="Book_Club", member_count=40))
    await bot.on_guild_remove(SimpleNamespace(id=2, name="Gone", member_count=None))
    assert told == [
        "Added to the server **Book\\_Club**, which has 40 members. The bot is now in 1 server.",
        "Removed from the server **Gone**. The bot is now in 1 server.",
    ]


async def test_stats_show_the_top_ten_books_ties_going_to_the_most_recent(
    tmp_path, capsys, monkeypatch
):
    path = str(tmp_path / "usage.db")
    log = UsageLog(path, "salt")
    for n in range(1, 13):
        await log.record(ORIGIN, [(f"book {n}", Book(id=100 + n, title=f"Book {n}"))], None)
    # Asked for by a second person, so first, though it was asked for earliest.
    other = Origin(**{**ORIGIN.__dict__, "user_id": 43})
    await log.record(other, [("book 1", Book(id=101, title="Book 1"))], None)
    log.close()
    monkeypatch.setattr("sys.argv", ["stats", "--include-owner", path])
    stats.main()
    section = capsys.readouterr().out.split("== Most mentioned books ==")[1].split("==")[0]
    titles = [line.split("  ")[0] for line in section.strip().splitlines()[1:]]
    assert titles == ["Book 1", *(f"Book {n}" for n in range(12, 3, -1))]
