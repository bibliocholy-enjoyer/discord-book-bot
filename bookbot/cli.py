"""Try lookups from a terminal, without Discord: python -m bookbot.cli "Dune by Frank Herbert" """

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv

from .hardcover import HardcoverClient
from .links import build_links
from .lookup import find_book


async def run(queries: list[str]) -> None:
    # trust_env picks up HTTPS_PROXY, for running behind a proxy such as a cloud sandbox
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = HardcoverClient(session, os.environ["HARDCOVER_TOKEN"])
        for query in queries:
            result = await find_book(client, query)
            print(f"{{{query}}}")
            if not result.book:
                print("  not found\n")
                continue
            book = result.book
            links = build_links(book, *await client.external_ids(book.id))
            series = f" [{book.series_name} #{book.series_position}]" if book.series_name else ""
            print(f"  {book.title} — {', '.join(book.author_names)} ({book.release_year}){series}")
            print(f"  {links.hardcover}\n  {links.goodreads}\n  {links.amazon}")
            for other in result.alternatives[1:]:
                print(f"  alt: {other.title} — {', '.join(other.author_names)}")
            print()


if __name__ == "__main__":
    load_dotenv()
    if len(sys.argv) < 2:
        raise SystemExit('usage: python -m bookbot.cli "Title by Author" ...')
    asyncio.run(run(sys.argv[1:]))
