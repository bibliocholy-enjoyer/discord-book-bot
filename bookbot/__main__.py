import logging

import discord
from dotenv import load_dotenv

from .bot import BookBot
from .config import Config


def main() -> None:
    # Values already set in the environment win over .env: that is how Fly's secrets
    # reach the bot, but it also means a stray `export HARDCOVER_TOKEN=...` in a
    # terminal silently overrides the file.
    load_dotenv()
    discord.utils.setup_logging(level=logging.INFO)
    config = Config.from_env()
    BookBot(config).run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
