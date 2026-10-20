"""Pull {book queries} out of Discord messages."""

import re

# Not more than 5: Discord allows five rows of controls on a message, and the "Wrong
# book?" picker needs a row per book. formatting.py also has one stripe color per book.
MAX_QUERIES_PER_MESSAGE = 5

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)
_BRACES = re.compile(r"\{([^{}\n]{2,150})\}")
# Things that show up inside braces in pasted code/JSON but not in book titles.
_CODE_LIKE = re.compile(r"""["']\s*:|=>|[=;<>\\]|^\s*[$%@]""")


def extract_queries(content: str) -> list[str]:
    """Return the distinct {…} queries in a message, in order, capped per message."""
    if "{" not in content:
        return []
    content = _CODE.sub(" ", content)
    queries: list[str] = []
    seen: set[str] = set()
    for match in _BRACES.finditer(content):
        query = " ".join(match.group(1).split())
        if not any(c.isalnum() for c in query) or _CODE_LIKE.search(query):
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        queries.append(query)
        if len(queries) == MAX_QUERIES_PER_MESSAGE:
            break
    return queries


_SEPARATORS = (
    re.compile(r"\s+by\s+", re.IGNORECASE),
    re.compile(r"\s+[-–—]\s+"),
    re.compile(r",\s+"),
)


def split_title_author(query: str) -> tuple[str, str] | None:
    """Guess a (title, author) split: "Dune by Frank Herbert" -> ("Dune", "Frank Herbert").

    Splits on the last " by ", else the last " - ", else the last ", ". The guess can be
    wrong ("Stand by Me"), so callers must fall back to searching the whole query.
    """
    for separator in _SEPARATORS:
        matches = list(separator.finditer(query))
        if not matches:
            continue
        last = matches[-1]
        title, author = query[: last.start()].strip(), query[last.end() :].strip()
        if title and author:
            return title, author
    return None
