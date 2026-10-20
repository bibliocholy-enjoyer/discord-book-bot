# BookBot privacy policy

*Effective October 17, 2026*

BookBot is a Discord bot that replies to `{book title}` in a message, or to the `/book`
command, with a card about the book. It is run by [NAME], an individual, not a company.
This page says what it reads, what it keeps, and who else sees any of it.

## What the bot reads

In a server it has been added to, the bot reads the messages in the channels it can see,
so that it can spot `{…}`. A message without `{…}` is ignored and nothing about it is
kept.

For a message with `{…}`, or a `/book` command, the bot reads the message, the title you
asked for, and who you are and where you asked, so that it can reply to you.

## What it keeps

To learn which books get asked about, the bot keeps a log of lookups made in servers.
Each entry holds:

- the title text you typed inside the braces, and the book it matched;
- when it was asked;
- the server, channel, category and thread it was asked in: their IDs and their names at
  the time;
- the ID of your message (for a `{…}` message, not for `/book`);
- whether you used the "Wrong book?" button to pick a different match;
- a rough positive or negative score for your message's tone. The bot works it out from
  the message's text the moment it arrives, with the titles left out, then discards the
  text. The score alone is kept;
- a code made from your Discord user ID with a secret key. It shows that two lookups were
  made by the same person, without saying who. It cannot be turned back into your ID by
  anyone who doesn't have the key, and only the operator has it.

It does **not** keep the text of your messages, your user ID, your username, or anything
from your profile.

Nothing is logged for lookups in direct messages, or for `/book` used from your own account
in a server that hasn't added the bot.

When you edit a `{…}` out of a message, its entry is removed from the log.

The log also keeps details of each book that was looked up (its title, authors, series
and so on) from [Hardcover](https://hardcover.app).

Separately, the bot's server keeps short-lived technical logs for fixing problems. These
can include a server's name and ID, and a link to a message that a lookup failed on.

## Why

The log shows the operator which books are popular, which lookups find the wrong book,
and which books are missing from Hardcover, so that they can be added there and the
replies that couldn't find them can be filled in. It is not sold, shared, or used for
advertising.

## Who else sees anything

- **Hardcover** receives the title you asked for, in order to find the book. Nothing
  about you or your server is sent with it.
- **Fly.io** hosts the bot and its log, in the United States.
- **Discord** delivers messages to and from the bot, under
  [Discord's own privacy policy](https://discord.com/privacy).

Nobody else receives any of it.

The book cards link to Hardcover, Goodreads and Amazon. Those links carry no information
about you, and there is no affiliate or tracking code in them, but those sites have their
own privacy policies once you click through.

## How long it is kept

Log entries are kept until they are deleted, either by the operator or at your request.
Removing the bot from a server stops new entries from that server but does not delete the
old ones. Ask, and they will be.

## Asking for your data to be deleted

Contact [CONTACT] with your Discord user ID, or a server admin with the server's ID, and
say what you want removed. Using the secret key, the operator can find every entry made by
your user ID, or every entry from a server, and will delete them.

## Children

Discord requires its users to be at least 13 (older in some countries). The bot is meant
for Discord users and does not knowingly collect anything from anyone younger.

## Changes

If this policy changes, the new version will be posted here with a new date at the top.

## Contact

[CONTACT]
