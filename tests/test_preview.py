import pytest

from bookbot.preview import build_page, to_html


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("plain", "<div>plain</div>"),
        ("### Big", '<div class="heading">Big</div>'),
        ("-# tiny", '<div class="small">tiny</div>'),
        ("**b** *i* __u__ ~~s~~", "<div><strong>b</strong> <em>i</em> <u>u</u> <s>s</s></div>"),
        ("[Dune](https://x.test/a)", '<div><a href="https://x.test/a">Dune</a></div>'),
        ("[Dune](<https://x.test/a>)", '<div><a href="https://x.test/a">Dune</a></div>'),
        (r"\*not italic\*", "<div>*not italic*</div>"),
        (r"[a \[b\]](https://x.test)", '<div><a href="https://x.test">a [b]</a></div>'),
        ("a < b & c", "<div>a &lt; b &amp; c</div>"),
        ("one\ntwo", "<div>one</div>\n<div>two</div>"),
    ],
)
def test_to_html(markdown, expected):
    assert to_html(markdown) == expected


def test_page_shows_each_kind_of_reply():
    page = build_page()
    assert (
        '<a href="https://hardcover.app/id/book/312460"><strong>Dune</strong> by Frank Herbert</a>'
        in page
    )
    assert "<div>1965 · 528 pages · Science Fiction, Classics</div>" in page
    assert "<div><em>Dune #1</em></div>" in page
    assert "Couldn't find <strong>Some Book Nobody Has Heard Of</strong>" in page
    assert "Dune — Frank Herbert (1965)" in page
    assert "border-left: 4px solid #e5484d" in page
