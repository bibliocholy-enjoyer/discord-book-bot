"""See what the bot's replies look like, in a web browser, without Discord.

    uv run python -m bookbot.preview

This builds a reply for the sample book below using the real code in formatting.py, draws
it roughly the way Discord would, and opens the result in your browser. No accounts or
keys are needed. Run it again after each change to formatting.py.

It is an imitation: the fonts, spacing and colors are close to Discord's but not exact,
and only the formatting listed in `to_html` below is understood. Discord itself is the
final word, so check there before trusting a change.
"""

import html
import re
import tempfile
import webbrowser
from pathlib import Path

from .formatting import alternative_label, book_embed, not_found_line
from .hardcover import Author, Book
from .links import Links

# The book shown in the preview. Change it to see how a card copes: set series_name=None,
# empty the genres, add more authors, or use a very long title.
SAMPLE_BOOK = Book(
    id=312460,
    title="Dune",
    authors=[Author(id=171873, name="Frank Herbert")],
    series_name="Dune",
    series_position=1.0,
    description="The blurb would go here. It is usually a few paragraphs long.",
    rating=4.33,
    ratings_count=6065,
    release_year=1965,
    pages=528,
    genres=["Science Fiction", "Classics", "Space Opera", "Adventure"],
    cover_url="https://assets.hardcover.app/edition/6216483/16b2e7f2-d173-46d4-932c-8c5158ea5eff.jpg",
)
SAMPLE_LINKS = Links(
    hardcover="https://hardcover.app/id/book/312460",
    goodreads="https://www.goodreads.com/book/show/10045",
    amazon="https://www.amazon.com/dp/B00B7NPRY8",
)
SAMPLE_QUERY_NOT_FOUND = "Some Book Nobody Has Heard Of"


def to_html(markdown: str) -> str:
    """Turn the bits of Discord's Markdown that the bot uses into HTML.

    Understands headings (###), small print (-#), **bold**, *italic*, __underline__,
    ~~strikethrough~~, [links](https://…), backslash escapes and line breaks.
    """
    lines = []
    for line in markdown.split("\n"):
        if line.startswith("### "):
            lines.append(f'<div class="heading">{_inline(line[4:])}</div>')
        elif line.startswith("-# "):
            lines.append(f'<div class="small">{_inline(line[3:])}</div>')
        else:
            lines.append(f"<div>{_inline(line) or '&nbsp;'}</div>")
    return "\n".join(lines)


def _inline(text: str) -> str:
    # A backslash means "show the next character as itself", so set those characters
    # aside while the formatting is worked out, and put them back at the end.
    kept: list[str] = []

    def keep(match: re.Match) -> str:
        kept.append(html.escape(match.group(1)))
        return f"\x00{len(kept) - 1}\x00"

    text = re.sub(r"\\(.)", keep, text)
    text = html.escape(text, quote=False)
    # [text](url) or [text](<url>); the angle brackets only tell Discord not to preview it.
    text = re.sub(
        r"\[([^\]]+)\]\((?:&lt;)?(https?://[^\s)]+?)(?:&gt;)?\)", r'<a href="\2">\1</a>', text
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<u>\1</u>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda match: kept[int(match.group(1))], text)


def card_html(embed) -> str:
    """The card's title field, linked if it has a link, followed by its text."""
    title = ""
    if embed.title:
        title = _inline(embed.title)
        if embed.url:
            title = f'<a href="{html.escape(embed.url)}">{title}</a>'
        title = f'<div class="heading">{title}</div>\n'
    return title + to_html(embed.description or "")


def build_page() -> str:
    embed = book_embed(SAMPLE_BOOK, SAMPLE_LINKS)
    stripe = str(embed.color) if embed.color else "#1e1f22"
    cover = ""
    if embed.thumbnail.url:
        cover = f'<img class="cover" src="{html.escape(embed.thumbnail.url)}" alt="cover">'
    return PAGE.format(
        stripe=stripe,
        card=card_html(embed),
        cover=cover,
        not_found=to_html(not_found_line(SAMPLE_QUERY_NOT_FOUND)),
        menu_entry=html.escape(alternative_label(SAMPLE_BOOK)),
    )


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Book bot preview</title>
<style>
  body {{ background: #313338; color: #dbdee1; margin: 32px;
         font: 16px/1.375 "gg sans", "Helvetica Neue", Helvetica, Arial, sans-serif; }}
  a {{ color: #00a8fc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #949ba4;
        margin: 32px 0 8px; }}
  .note {{ color: #949ba4; font-size: 14px; max-width: 520px; }}
  .card {{ background: #2b2d31; border-left: 4px solid {stripe}; border-radius: 4px;
           padding: 12px 16px; max-width: 432px; display: flex; gap: 16px;
           font-size: 14px; line-height: 1.3; }}
  .card .text {{ flex: 1; min-width: 0; }}
  .heading {{ font-size: 16px; font-weight: 500; line-height: 1.375; margin: 0 0 4px; }}
  .small {{ font-size: 12px; color: #949ba4; }}
  .cover {{ width: 80px; height: 80px; object-fit: contain; border-radius: 4px; }}
  .buttons {{ margin-top: 8px; display: flex; gap: 8px; }}
  .button {{ background: #4e5058; color: #fff; border-radius: 8px; padding: 6px 16px;
             font-size: 14px; font-weight: 500; }}
  .menu {{ background: #1e1f22; border-radius: 8px; padding: 10px 12px; max-width: 432px;
           font-size: 14px; }}
</style>

<p class="note">An imitation of Discord, drawn from the real code in formatting.py. The
spacing and fonts are close but not exact. Run the command again after each change.</p>

<h2>A book card</h2>
<div class="card">
  <div class="text">
{card}
  </div>
  {cover}
</div>
<div class="buttons">
  <span class="button">Wrong book?</span><span class="button">🗑️ Delete</span>
</div>

<h2>When a book isn't found</h2>
<div>{not_found}</div>

<h2>An entry in the "Wrong book?" menu</h2>
<div class="menu">{menu_entry}</div>
"""


def main() -> None:
    embed = book_embed(SAMPLE_BOOK, SAMPLE_LINKS)
    print("The card's title and text, before Discord formats them:\n")
    print(embed.title)
    print(embed.description)
    print("\nWhen a book isn't found:\n")
    print(not_found_line(SAMPLE_QUERY_NOT_FOUND))
    print('\nAn entry in the "Wrong book?" menu:\n')
    print(alternative_label(SAMPLE_BOOK))

    page = Path(tempfile.gettempdir()) / "bookbot-preview.html"
    page.write_text(build_page(), encoding="utf-8")
    print(f"\nOpening a preview in your browser: {page}")
    webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
