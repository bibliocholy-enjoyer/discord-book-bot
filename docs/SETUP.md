# Setting up the bot from scratch

Everything needed to get this bot running with nothing but a copy of this repository:
the accounts, the keys, the Discord application, and the deployment. It is written so
that someone other than the original author can do it.

If you are taking the bot over rather than starting fresh, read
[Taking over from someone else](#taking-over-from-someone-else) first.

## What you need

Accounts (all free except Fly):

| Account | Used for |
|---|---|
| [Hardcover](https://hardcover.app) | The book data. The API key belongs to one person's account. |
| [Discord](https://discord.com) | Owning the bot's application in the developer portal. |
| [Fly.io](https://fly.io) | Hosting. Needs a payment card; expect a few dollars a month. |
| [healthchecks.io](https://healthchecks.io) | Optional. Emails you if the bot goes down. |
| GitHub | Optional. Automatic deploys when code is pushed to `main`. |

On your computer:

- [git](https://git-scm.com/downloads)
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs the right
  Python and the bot's dependencies
- [flyctl](https://fly.io/docs/flyctl/install/), Fly's command-line tool

Docker is not needed; Fly builds the image on its own servers.

## 1. Get the code

```bash
git clone git@github.com:bibliocholy-enjoyer/discord-book-bot.git
cd discord-book-bot
uv sync
uv run pytest
```

The tests need no keys and should all pass.

## 2. Create a Hardcover API key

1. Sign in to Hardcover and open [Account → Hardcover API](https://hardcover.app/account/api).
2. Click **New API Key**.
3. Name it something like `discord-book-bot`.
4. Expiration: the longest offered ("never" if available). When a key expires the bot
   stops finding books and DMs its owner; you then repeat this step.
5. Permissions: tick **`read:catalog`** and nothing else. That covers searching and
   reading book details. Leaving everything else off means a leaked key can't touch your
   Hardcover account.
6. Copy the key. It is shown once.

The free tier allows 5,000 requests a day and 60 a minute. A lookup uses 3 to 5, so that
is roughly a thousand lookups a day. Hardcover's API is in beta: they may change it or
reset keys without notice.

## 3. Create the Discord application

Do this once for the real bot. If you will also work on the code, do it a second time for
a development copy (for example "BookBotDev"), so that a bot running on your laptop and
the deployed one never both answer the same message.

In the [Discord developer portal](https://discord.com/developers/applications):

1. **New Application**. The name is what people see above every reply.
2. **General Information**:
   - Description: shown as the bot's "About Me". Say how to use it and who to contact,
     for example: *Put a book in curly braces, like {The Everlasting by Alix Harrow}, and
     I'll reply with links to Hardcover, Goodreads and Amazon. /book works too.*
   - App icon: `assets/avatar.png` (or `assets/avatar-dev.png` for the development copy).
3. **Bot** tab:
   - Icon and banner: the same avatar, and `assets/banner.png` (or `banner-dev.png`).
   - **Reset Token** and copy the token. It is shown once. This is `DISCORD_TOKEN`.
     (The "public key" on the General Information page is something else and isn't used.)
   - **Public Bot**: on, unless you are the admin of every server it will join. Whoever
     adds a bot to a server needs Manage Server there, and with this off only the
     application's owner can add it. Turning it on does not list the bot anywhere.
   - **Message Content Intent**: on. Without it the bot cannot see `{…}` in messages and
     will refuse to start.
   - Presence Intent and Server Members Intent: off.
   - Leave the "Bot Permissions" checkboxes alone. They are only a calculator.
4. **Installation** tab:
   - **Installation Contexts**: turn on both **User Install** and **Guild Install**.
   - **Install Link**: **Discord Provided Link**.
   - **Default Install Settings**: leave User Install's scopes at `applications.commands`.
     Give Guild Install the scopes `applications.commands` and `bot`, and the six
     permissions in the table under [Invite the bot](#invite-the-bot).
   - **Save Changes**.
5. **Emojis**: upload `assets/bookbot_error.png` and name it exactly `bookbot_error`. The
   bot reacts with it when a lookup fails. Without it, it uses ⚠️.

### Invite the bot

Give out the link shown under **Install Link** on the **Installation** tab. It looks like
`https://discord.com/oauth2/authorize?client_id=…`, and clicking **Add App** on the bot's
profile in Discord does the same thing. It offers two choices:

- **Add to Server**: the bot joins a server, and `{…}` and `/book` work there. It lists
  the servers where the person has Manage Server.
- **Add to My Apps**: `/book` works for that person anywhere, including DMs and servers
  without the bot. `{…}` still only works where the bot itself is present, because that
  is the only place it can read messages.

The link holds only the application's ID; Discord fills in the scopes and permissions
from the Default Install Settings when it is opened. So links already given out pick up
any later change to those settings.

Adding it to a server asks for six permissions and nothing else:

| Permission | Why |
|---|---|
| View Channels | See messages, to spot `{…}` |
| Send Messages | Post the reply |
| Send Messages in Threads | Reply inside threads |
| Embed Links | Show the book card; replies fail without it |
| Read Message History | Attach the reply to the message that asked |
| Add Reactions | React to a message when its lookup fails |

To keep the bot out of a channel, a server admin removes its View Channel permission
there.

## 4. Run it on your own computer

```bash
cp .env.example .env
```

Fill in `.env`:

| Setting | Value |
|---|---|
| `DISCORD_TOKEN` | The bot token from step 3 (the development copy's, if you made one) |
| `HARDCOVER_TOKEN` | The key from step 2 |
| `HEARTBEAT_URL` | Leave empty locally |
| `OWNER_ID` | Leave empty to DM errors to the application's owner; see below |
| `SUPPORT_URL` | Optional: an invite to the bot's support server, shown at the end of `/help`. The deployed bot's is set in `fly.toml` |

`.env` is ignored by git. Never commit it or paste its contents anywhere.

```bash
uv run python -m bookbot
```

It should log `Connected as <name> in 1 server(s)`. Then, in a server the bot is in:

- `{The Everlasting by Alix Harrow}` → a card with the cover and three links
- `{Dune} and {Circe}` → two cards in one reply
- `{asdkjfh qwerty zzz}` → "Couldn't find … on Hardcover", with search links
- `/book Dune` → the same card (the command can take a minute to appear the first time)
- **Wrong book?** → a private menu; picking another match changes the card
- 🗑️ → deletes the reply
- Edit `{Dune}` to `{Circe}` in a message → its reply changes to Circe, in place

To check lookups without Discord:

```bash
uv run python -m bookbot.cli "Dune by Frank Herbert" "everlasting"
```

To see the failure path, start the bot with a bad Hardcover key. A lookup should then get
the error reaction and nothing else in the channel, and the owner should get a DM:

```bash
HARDCOVER_TOKEN=wrong uv run python -m bookbot
```

Stop the bot with Ctrl+C.

**Who gets the error DMs:** the application's owner, or the owner of its team. To send
them to someone else, set `OWNER_ID` to that person's Discord user ID (in Discord:
Settings → Advanced → Developer Mode, then right-click the person → Copy User ID). They
must share a server with the bot and allow DMs from that server's members.

## 5. Set up the heartbeat (optional, recommended)

The bot can DM you about errors, but not if it is down. For that:

1. At [healthchecks.io](https://healthchecks.io), create a check.
2. Period: 5 minutes. Grace time: 10 minutes.
3. Copy its ping URL. This is `HEARTBEAT_URL`.

The bot requests that URL every 5 minutes while it is connected to Discord. If the
requests stop, healthchecks.io emails you.

## 6. Deploy to Fly.io

> This section was written before the bot's first deployment and has not yet been run end
> to end. If a step turns out differently, please correct it here.

```bash
fly auth signup     # or: fly auth login
```

Create the app. Names are unique across all of Fly, so if `discord-book-bot` is taken,
pick another and change the `app` line in `fly.toml` to match:

```bash
fly apps create discord-book-bot
```

Create the volume that holds the usage log (see
[HOW_IT_WORKS.md](HOW_IT_WORKS.md#what-the-bot-remembers)), and keep its daily snapshots
for 30 days instead of the default 5. Skip this and the `USAGE_SALT` below to run without
a log, and delete the `[mounts]` section and the `USAGE_DB` line of `fly.toml`:

```bash
fly volumes create bookbot_data --size 1 --region iad
fly volumes list                                  # note the volume's ID
fly volumes update <volume id> --snapshot-retention 30
```

`USAGE_SALT` is what makes the hashes in the usage log unguessable. Make one and **save it
in a password manager before going on**: `fly secrets` never shows a secret's value again.
Never change it afterwards, or the same person gets a different hash and counts as someone
new.

```bash
openssl rand -hex 32
```

Give the app its secrets, using the real bot's token, not the development copy's. Leave out
`HEARTBEAT_URL` if you skipped step 5, and add `OWNER_ID=...` if you need it. Paste the salt
you just saved:

```bash
fly secrets set DISCORD_TOKEN=... HARDCOVER_TOKEN=... HEARTBEAT_URL=... USAGE_SALT=...
```

(If you ever lose the copy, a running bot can still tell you:
`fly ssh console -C "printenv USAGE_SALT"`.)

Deploy, and make sure exactly one copy is running. Two copies would each answer every
message:

```bash
fly deploy --ha=false
fly scale count 1
```

Check it:

```bash
fly status
fly logs
```

The logs should show `Connected as <name> in N server(s)`. Try a `{…}` in the server.

### Automatic deploys from GitHub

`.github/workflows/ci.yml` runs the tests on every pull request and every push to `main`,
and deploys after a push to `main` if they pass. A push that only changes Markdown files
(the README and `docs/`) is skipped, since none of that goes into the bot. For the deploy
to work, the repository needs a secret holding a Fly token:

```bash
fly tokens create deploy
```

Save the output in GitHub under the repository's Settings → Secrets and variables →
Actions → New repository secret, named `FLY_API_TOKEN`. Only the repository's owner can
do this; a collaborator cannot.

Without that secret the tests still run but the deploy step fails. You can always deploy
by hand with `fly deploy`.

## Day to day

| Task | How |
|---|---|
| See what the bot is doing | `fly logs` |
| Restart it | `fly apps restart discord-book-bot` |
| Replace a key or token | `fly secrets set NAME=value` (restarts the bot) |
| Deploy a code change | Merge to `main`, or run `fly deploy` |
| Stop it | `fly scale count 0` (and `fly scale count 1` to start again) |

Every restart makes the bot forget the other matches behind existing **Wrong book?**
buttons. On a reply to a message, clicking one afterwards searches again, so it takes a
moment longer. On a `/book` reply it removes the button with a note. 🗑️ keeps working.

## Troubleshooting

| What you see | Likely cause | Fix |
|---|---|---|
| Error reaction on every lookup; DM mentions `HTTP 401` | Hardcover key expired, was reset, or is wrong | New key (step 2), then `fly secrets set HARDCOVER_TOKEN=...` |
| DM mentions `HTTP 403` and a scope | The key lacks `read:catalog` | New key with that permission |
| DM mentions `Too Many Requests` | Hardcover's daily limit was reached | Wait for the next day; ask Hardcover for a higher limit if it recurs |
| Bot exits at startup with `PrivilegedIntentsRequired` | Message Content Intent is off | Turn it on (step 3) |
| Bot exits at startup with `LoginFailure` | Wrong or reset Discord token | Reset the token (step 3), then `fly secrets set DISCORD_TOKEN=...` |
| Every lookup gets two replies | Two copies are running with the same token | `fly scale count 1`; stop any copy on a laptop |
| Bot is online but ignores a channel | It can't view or post in that channel | Check the channel's permissions for the bot |
| Replies fail; DM mentions `Missing Permissions` | Embed Links or Send Messages was removed | Restore the permissions in step 3 |
| `/book` doesn't appear | Registration is still propagating | Wait a few minutes and restart Discord |
| Heartbeat email arrives | The bot is down or disconnected | `fly status` and `fly logs` |
| A cover is missing for a moment | Discord hadn't fetched it yet | Nothing; the bot re-sends the card up to three times |
| "Couldn't find" for a book that exists | It isn't on Hardcover, or the query is too far from the title | Add the author, or check hardcover.app |

## Taking over from someone else

Several pieces belong to one person's accounts, and a GitHub collaborator does not get
access to them:

| Piece | Tied to | If that person is unavailable |
|---|---|---|
| Hardcover API key | Their Hardcover account | Make your own (step 2) |
| Discord application | Their Discord account, unless it was moved to a team | Make a new application (step 3), invite it, and remove the old bot from the server |
| Fly app | Their Fly account, unless you were added to their organization | Deploy under your own account (step 6) |
| Heartbeat check | Their healthchecks.io account | Make your own (step 5) |
| GitHub deploy secret | The repository owner | Fork the repository and add the secret to your fork, or deploy by hand |

So a full takeover is: fork or clone the repository, then steps 2 to 6 with your own
accounts. The new bot is a different Discord user from the old one, so it has to be
invited again, and the old one should be kicked so that both don't answer. Nothing else
carries over, because the bot stores no data.

Two things the current owner can do in advance to make this smoother:

- **Discord:** create a team in the developer portal (Teams → New Team), invite the other
  person, and transfer the application to the team. Either member can then manage the
  bot and reset its token, and the bot keeps its identity.
- **Fly:** invite the other person to the Fly organization that owns the app, so they can
  deploy, read logs and change secrets.
