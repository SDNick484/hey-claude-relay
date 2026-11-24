"""Make a reply fit to be read aloud: take the markdown out.

The voice prompt asks the model for plain text, and it mostly complies. This is the backstop,
so a stray list or link is spoken as words instead of as "asterisk asterisk".
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+•]\s+(?:\[[ xX]\]\s+)?(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,3}[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")

# The lengths are bounded so that a flood of unclosed brackets costs time in proportion to its
# length, not to its square: this runs on whatever the model wrote, on the event loop.
_LINK = re.compile(
    r"!?\[([^\]\n]{0,300})\]"
    r"\([^()\s]{0,2000}(?:\([^()\s]{0,200}\)[^()\s]{0,2000}){0,4}(?:\s+\"[^\"\n]{0,300}\")?\)"
)
_REF_LINK = re.compile(r"!?\[([^\]\n]{1,300})\]\[[^\]\n]{0,300}\]")
_AUTOLINK = re.compile(r"<https?://[^>\s]+>")
_URL = re.compile(r"https?://[^\s<>]*[^\s<>.,;:!?)\]'\"]")
# An opening marker must touch the text it wraps, and so must the closing one. That keeps
# "5 * 3" and snake_case names as they are.
_EMPHASIS = (
    re.compile(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)([^\n]+?)(?<=\S)__(?!\w)"),
    re.compile(r"~~(?=\S)([^\n]+?)(?<=\S)~~"),
    re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"),
    re.compile(r"(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])"),
)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([.,;:!?])")
_SPACES = re.compile(r"\s+")

_ENDS_A_CLAUSE = ".!?:;,"


def _inline(text: str) -> str:
    if "](" in text:
        text = _LINK.sub(r"\1", text)
    if "][" in text:
        text = _REF_LINK.sub(r"\1", text)
    text = _AUTOLINK.sub("", text)
    text = _URL.sub("", text)
    for pattern in _EMPHASIS:
        text = pattern.sub(r"\1", text)
    text = text.replace("`", "")
    text = _EMPTY_BRACKETS.sub("", text)
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    return _SPACES.sub(" ", text).strip()


def strip_markdown(text: str) -> str:
    """``text`` as one line of plain words.

    Headings, list items and table rows each become a sentence. Links keep their words and lose
    their address; bare addresses are dropped. Lines are joined with a full stop where one line
    ends without punctuation.
    """
    pieces: list[tuple[str, bool]] = []  # (words, whether the line was a heading, list item or table row)
    lines = text.replace("\x00", " ").splitlines()
    # "1. Chili / 2. Risotto" is a list. A single line that starts with a number is an answer
    # ("45. Anything else?"), and its number must not be taken for a list marker and dropped.
    # So a numbered line is a list item only in company: among other numbered or bulleted lines.
    numbered_list = sum(1 for line in lines if _NUMBERED.match(line) or _BULLET.match(line)) >= 2
    for line in lines:
        if _FENCE.match(line) or _TABLE_RULE.match(line) or _RULE.match(line):
            continue
        line = _QUOTE.sub("", line)
        structural = True
        if heading := _HEADING.match(line):
            words = heading.group(1)
        elif item := _BULLET.match(line) or (_NUMBERED.match(line) if numbered_list else None):
            words = item.group(1)
        elif line.strip().startswith("|") and line.strip().endswith("|") and len(line.strip()) > 1:
            words = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|") if cell.strip())
        else:
            words, structural = line, False
        words = _inline(words)
        if words:
            pieces.append((words, structural))

    spoken = ""
    for words, structural in pieces:
        if structural and words[-1] not in _ENDS_A_CLAUSE:
            words += "."
        if spoken:
            spoken += " " if spoken[-1] in _ENDS_A_CLAUSE else ". "
        spoken += words
    return spoken
