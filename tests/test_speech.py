import pytest

from hey_claude_relay.speech import strip_markdown


@pytest.mark.parametrize(
    "raw, spoken",
    [
        ("**Paddington 2** is on *Shelf 2*.", "Paddington 2 is on Shelf 2."),
        ("# Tonight\n\n- Chili\n- Risotto", "Tonight. Chili. Risotto."),
        ("1. Chili\n2. Risotto", "Chili. Risotto."),
        ("See [the recipe](https://example.com/r/1) for more.", "See the recipe for more."),
        ("More at https://example.com/x.", "More at."),
        ("Use `ha_get_state` now", "Use ha_get_state now"),
        ("```\ncode\n```\nDone.", "code. Done."),
        ("| A | B |\n|---|---|\n| 1 | 2 |", "A, B. 1, 2."),
        ("  spaced   out \n\n text ", "spaced out. text"),
        ("It's 5 * 3 = 15.", "It's 5 * 3 = 15."),
    ],
)
def test_strip_markdown(raw, spoken):
    assert strip_markdown(raw) == spoken


@pytest.mark.parametrize(
    "raw, spoken",
    [
        ("", ""),
        ("   \n\n  ", ""),
        ("Plain sentence. Another one!", "Plain sentence. Another one!"),
        ("Three options:\n- Chili\n- Risotto\n- Soup", "Three options: Chili. Risotto. Soup."),
        ("__Bold__ and _italic_ and ~~gone~~ words", "Bold and italic and gone words"),
        ("Call ha_get_state or the_other_tool, not _this_.", "Call ha_get_state or the_other_tool, not this."),
        ("**Chili** and **risotto** both work.", "Chili and risotto both work."),
        ("![poster](https://example.com/p.jpg) Paddington 2", "poster Paddington 2"),
        ("> Quoted line\n> second", "Quoted line. second"),
        ("Before\n\n---\n\nAfter", "Before. After"),
        ("- [x] Done thing\n- [ ] Open thing", "Done thing. Open thing."),
        ("* One\n  * Nested\n+ Two\n3) Three", "One. Nested. Two. Three."),
        ("## Dinner ##\nChili.", "Dinner. Chili."),
        ("Details at <https://example.com/x> today.", "Details at today."),
        ("It is at (https://example.com/a) now", "It is at now"),
        ("See [the docs][1] for more.", "See the docs for more."),
        ("2 * 3 * 4 is 24", "2 * 3 * 4 is 24"),
        ("Set it to 70%, then wait.", "Set it to 70%, then wait."),
        ("First line,\nsecond line", "First line, second line"),
        ("~~~\nx = 1\n~~~", "x = 1"),
        ("| Movie | Shelf |\n| :--- | ---: |\n| Up | 2 |", "Movie, Shelf. Up, 2."),
    ],
)
def test_strip_markdown_more(raw, spoken):
    assert strip_markdown(raw) == spoken


@pytest.mark.parametrize(
    "raw", ["****", "[](", "![]()", "|", "```", "`", "#", "- ", "\x00\n\t", "[a](b" * 500, "*" * 1000]
)
def test_strip_markdown_never_raises_and_stays_on_one_line(raw):
    out = strip_markdown(raw)
    assert "\n" not in out and out == out.strip()
