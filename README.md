# discord-book-bot

A Discord bot that replies to `{book title}` or `{book title by author}` with a compact
card about the book from [Hardcover](https://hardcover.app): cover, author, series, genres,
and links to the book on Hardcover, Goodreads and Amazon.

- Several `{…}` in one message get one combined reply (up to 5 books).
- `/book` does the same lookup as a slash command. Add the bot to your own account and
  `/book` works anywhere, including DMs and servers that don't have the bot.
- `/help` explains all this, privately, with the link for adding the bot.
- Editing a message updates its reply: add a `{…}` to get one, change it to swap the book,
  or remove it to take the reply away.
- The reply has a 🗑️ button so the person who asked can delete it, and a "Wrong book?"
  button that privately offers them the other matches. On a reply to a message it keeps
  working, even after the bot restarts; on a `/book` reply it is there for 14 minutes.
- The card's title links to the book on Hardcover, unless Hardcover has no cover or no
  blurb for it. Then it links to the book's Goodreads page, or its Amazon page, instead,
  or failing both, a Goodreads search. Once the cover and blurb are filled in on
  Hardcover, `/retry-missed` adds the cover to those cards and points their titles back
  at Hardcover.
- If a book isn't on Hardcover, the reply links to Goodreads and Amazon searches instead.
  Once the book has been added there, the bot's owner can run `/retry-missed` to put it
  into the replies that couldn't find it (see
  [Filling in books that weren't found](#filling-in-books-that-werent-found)).
- Replies never ping anyone.
- Optionally logs which books are asked for, and where, to a SQLite file (never message text
  or user IDs). See [Looking at the stats](#looking-at-the-stats).

## Getting started on a Mac

Starting from a Mac with [Homebrew](https://brew.sh) installed, this gets you a working
copy of the code that you can run the tests on and experiment with. No keys or accounts
are needed beyond GitHub.

1. Install the two tools the project needs:

   ```bash
   brew install uv gh
   ```

   - `uv` runs the project. It also downloads the right version of Python (3.13, named in
     the `.python-version` file) by itself the first time you use it, so you do **not**
     need pyenv or a separately installed Python.
   - `gh` is GitHub's command-line tool, used below to sign in and download the code.
   - `git` is already on your Mac: installing Homebrew installed it.

2. Sign in to GitHub, since this repository is private. Choose *GitHub.com*, then *HTTPS*,
   then *Login with a web browser*, and follow the prompts:

   ```bash
   gh auth login
   ```

3. Download the code and go into its folder:

   ```bash
   gh repo clone bibliocholy-enjoyer/discord-book-bot
   cd discord-book-bot
   ```

4. Install Python and the libraries the bot uses. They go into a hidden `.venv` folder
   inside the project and don't affect anything else on your Mac:

   ```bash
   uv sync
   ```

5. Check that everything works by running the tests. They should all pass:

   ```bash
   uv run pytest
   ```

6. See what the bot's replies look like, in your browser:

   ```bash
   uv run python -m bookbot.preview
   ```

Every command for this project starts with `uv run`, which runs it with the project's own
Python and libraries. There is nothing to "activate" first.

Any text editor works for changing the code. [Visual Studio Code](https://code.visualstudio.com)
is a good free one (`brew install --cask visual-studio-code`).

Where to go next:

- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) explains how the bot works, for someone new
  to programming.
- `bookbot/formatting.py` controls what replies look like and is the easiest place to
  start changing things. It has a guide at the top.
- Running the real bot needs keys for Discord and Hardcover; see
  [docs/SETUP.md](docs/SETUP.md).

## How it finds a book

Searching Hardcover for `Title by Author` verbatim tends to return study guides and box
sets. So when a query looks like it names an author (`… by …`, `… - …`, `…, …`) the bot
searches the title alone and picks the first result by that author, tolerating typos.
Otherwise, or if that finds nothing, it searches the whole query as written, and prefers a
popular book whose title is the query give or take a leading "The" (`{everlasting}` finds
*The Everlasting*). Punctuation is ignored when judging a match, so `{forget me not}` finds
a title spelled *Forget-Me-Not*.

## Running locally

This starts the bot on your own computer. While the command is running, the bot is online
in Discord and answers `{…}` messages and `/book` in every server it has been added to,
using the code as it is on your machine. That makes it the way to try out a change before
deploying it.

Which bot comes online depends on the `DISCORD_TOKEN` in `.env`. Use the token of a
separate development bot, not the real one's, so that the two don't both answer the same
message. [docs/SETUP.md](docs/SETUP.md) covers creating one and getting the tokens.

Requires [uv](https://docs.astral.sh/uv/).

One-time setup:

```bash
cp .env.example .env   # then fill in the tokens
uv sync
```

Start the bot:

```bash
uv run python -m bookbot
```

It logs `Connected as <name> in 1 server(s)` once it is online, and keeps running until you
press Ctrl+C, which takes the bot offline again. After changing the code, stop it and start
it again to pick up the change.

Try lookups without Discord (needs `HARDCOVER_TOKEN`):

```bash
uv run python -m bookbot.cli "Dune by Frank Herbert" "The Everlasting"
```

Preview what replies look like in a browser (needs no keys):

```bash
uv run python -m bookbot.preview
```

Tests and lint:

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## When something breaks

- A failed lookup gets a reaction on the message that asked, and nothing else in the channel.
  The reaction is ⚠️ unless the application has a custom emoji named `bookbot_error`
  (upload `assets/bookbot_error.png` under Emojis in the developer portal, for each application).
- The bot DMs its owner (the application owner, or `OWNER_ID`) with the error and a link to
  the message, at most once an hour per distinct error. You must share a server with the bot
  to receive DMs.
- If `HEARTBEAT_URL` is set, the bot requests it every 5 minutes while connected to
  Discord. Point it at a check on a service like [healthchecks.io](https://healthchecks.io)
  (period 5 minutes, grace 10) to get an email when the bot is down.

## How it works

[docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md) is a tour for someone new to programming: what
happens when a message arrives, where each part lives in the code, and how the bot gets
deployed, with diagrams.

## Filling in books that weren't found

When a `{…}` finds nothing, the usual fix is to add the book to Hardcover. After that,
run `/retry-missed` in a DM with the bot (click the bot's name, then **Message**). It
searches again for every query from the last 24 hours that found nothing (`hours:`
changes how far back, up to a year) and edits each reply to show the book.

It does the same for books that were found but had no cover or no blurb on Hardcover:
fill those in there, run `/retry-missed`, and each card for the book gets the cover, with
its title linking to Hardcover again.

Only you can see its answer, which says how many queries, books and replies it went
over, links to the replies it updated, and lists what is still missing.

- Only the bot's owner can use it, and it is not offered in servers at all, only in a DM
  with the bot.
- It needs the usage log, which is where the missed queries are kept.
- It covers `{…}` messages, not `/book` replies.
- A book added to Hardcover moments ago may not be in its search yet; run it again later.

## Looking at the stats

If the bot has a usage log (see [what it records](docs/HOW_IT_WORKS.md#what-the-bot-remembers)),
print a summary of it from your own computer. It starts with where the bot is installed
right now (every server, and how many people have added it to their own account), then
shows the ten most mentioned books (counting different people first, then mentions, then
how recently they were asked for), servers, channels, and searches that found nothing:

```bash
scripts/stats
```

The report is longer than a screen, so it opens in `less`: scroll with the arrow keys or
the space bar, and press `q` to leave.

### What's left out, and how to put it back

So that the report shows how other people use the bot, by default it leaves out:

- the bot's own servers, **BookBot test server** and **BookBot HQ**, and
- the books you asked for yourself, wherever you asked for them.

The bot's own servers are matched by ID, so renaming them changes nothing. To add one,
put its ID and name in `LEFT_OUT_BY_DEFAULT` at the top of `bookbot/stats.py` (with
Developer Mode on in Discord's Advanced settings, right-click a server's icon and pick
**Copy Server ID**).

The top of the report says what it left out. To change that:

| To see | Run |
|---|---|
| Everything, nothing left out | `scripts/stats --all-servers --include-owner` |
| The bot's own servers too | `scripts/stats --all-servers` |
| Your own books too | `scripts/stats --include-owner` |
| Only your own books | `scripts/stats --only-owner` |
| Only one server, even one of the bot's own | `scripts/stats --server 'BookBot HQ'` |
| Leave out more servers | `scripts/stats --exclude 'Some Server'` |

`--server` and `--exclude` can be repeated, and everything combines: `--server 'BookBot
HQ' --only-owner` is just what you asked for in BookBot HQ. Naming servers with
`--server` means nothing is left out by default, since you picked them.

Servers are matched by their exact name (capitals don't matter) or by their ID, which the
"Servers" section of an `--all-servers` run lists. The filters also apply to the list of
servers the bot is installed in, but not to the count of personal accounts, which aren't
in any server.

"You" is `OWNER_ID` if that is set, and otherwise the owner of the bot's application. The
count of personal accounts can't leave you out or pick you out, since Discord only gives
it as a number; the report says so next to it.

`scripts/stats` is a shortcut for running the report on Fly and piping it into `less`.
Without it, the first example in the table is:

```bash
fly ssh console -C "python -m bookbot.stats --all-servers --include-owner" | less
```

### From Discord

You can also get the report without a terminal: run `/stats` in a DM with the bot. It
answers with the same report, with each table row as a line that reads well on a phone,
spread over a few messages if it's long. The answer stays in the DM like any other
message, so scrolling back shows what the stats were each time you asked; delete a
report like any message if you don't want it.

It leaves out the same things by default, and takes the same filters as options:

- `server:` only these servers, and `exclude:` leave these out as well. A slash command
  can't repeat an option, so give several servers separated by commas:
  `server: BookBot HQ, 123456789012345678`.
- `all_servers: True` keeps the bot's own servers in.
- `owner:` is `leave out` (the default), `include`, or `only`.

Like `/retry-missed`, only the bot's owner can use it, and only in a DM with the bot.
Merging duplicate books still needs `fly ssh console` (see
[HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md#what-the-bot-remembers)).

The bot also DMs its owner whenever it is added to a server or removed from one.

## Setting up and deploying

[docs/SETUP.md](docs/SETUP.md) walks through everything from scratch: the Hardcover API
key, the Discord application and its settings, the invite link, the heartbeat, deploying
to Fly.io, troubleshooting, and how someone else can take the bot over.

In short: the bot needs `DISCORD_TOKEN` and `HARDCOVER_TOKEN`, runs as a single small
machine on Fly.io, and is deployed by GitHub Actions on every push to `main`.
