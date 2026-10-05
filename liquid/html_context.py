"""Context-aware HTML escaping for Liquid templates.

This module implements an explicitly opt-in contextual escaping mode. Instead of
applying a single HTML escape everywhere, literal template output is tracked
through a small HTML state machine, so each output statement can be escaped
according to the HTML context it appears in.

Only three output contexts are supported:

- `HTMLContextKind.TEXT`: plain HTML text.
- `HTMLContextKind.ATTR`: inside a quoted HTML attribute value.
- `HTMLContextKind.URL`: inside a quoted URL attribute value (like `href`).

Any other position (dynamic tag names, unquoted attribute values, script/style
content, HTML comments, etc.) is unsupported and rejected, either at parse time
or when reached during rendering. We never guess an escaping strategy.
"""

from __future__ import annotations

import re
from enum import Enum
from enum import auto
from html import unescape
from typing import Optional
from typing import Tuple

from markupsafe import escape as _escape_markup

from .exceptions import HTMLContextError
from .token import Token


class HTMLContextKind(Enum):
    """HTML contexts in which dynamic output is supported."""

    TEXT = auto()
    ATTR = auto()
    URL = auto()


class _State(Enum):
    """Internal states of the HTML context tracker."""

    TEXT = auto()
    TAG_NAME = auto()
    TAG = auto()
    ATTR_NAME = auto()
    AFTER_ATTR_NAME = auto()
    BEFORE_ATTR_VALUE = auto()
    ATTR_VALUE_QUOTED = auto()
    ATTR_VALUE_UNQUOTED = auto()
    RAWTEXT = auto()  # script and style content
    RCDATA = auto()  # title and textarea content
    COMMENT = auto()
    DECL = auto()  # doctype, CDATA and bogus comments


# Attributes whose values are URLs. Output statements inside quoted values of
# these attributes get URL protocol validation in addition to attribute escaping.
URL_ATTRIBUTES = frozenset(
    {
        "action",
        "background",
        "cite",
        "data",
        "formaction",
        "href",
        "poster",
        "src",
    }
)

# URL schemes allowed in URL attributes. Values without a scheme (relative URLs,
# fragments and queries) are always allowed.
ALLOWED_URL_SCHEMES = frozenset(
    {
        "cid",
        "ftp",
        "ftps",
        "http",
        "https",
        "mailto",
        "sms",
        "tel",
        "xmpp",
    }
)

_RAWTEXT_TAGS = frozenset({"script", "style"})
_RCDATA_TAGS = frozenset({"title", "textarea"})

_WHITESPACE = " \t\n\r\f"
_TAG_NAME_END = _WHITESPACE + "/>"
_ATTR_NAME_END = _WHITESPACE + "=/>"

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

# Characters browsers ignore when parsing a URL. Leading and trailing C0
# controls and space are stripped, and tab, LF, CR and NUL are removed from
# anywhere in the URL, including from within the scheme.
_URL_EDGE_STRIP = "".join(chr(c) for c in range(0x21))
_URL_EMBEDDED_STRIP = "\t\n\r\x00"


def _is_ascii_alpha(char: str) -> bool:
    return char.isascii() and char.isalpha()


class HTMLContextTracker:
    """Track HTML context through literal template output.

    Literal template text is fed to the tracker with `feed()`. At any point,
    `output_kind()` returns the `HTMLContextKind` a dynamic output statement
    would appear in, or `None` if that position is unsupported.
    """

    __slots__ = (
        "state",
        "tag_name",
        "attr_name",
        "quote",
        "_closing",
        "_raw",
        "_buf",
    )

    def __init__(self) -> None:
        self.state = _State.TEXT
        self.tag_name = ""
        self.attr_name = ""
        self.quote = ""
        self._closing = False
        self._raw = ""
        self._buf = ""

    def snapshot(self) -> Tuple[object, ...]:
        """Return an opaque snapshot of the tracker's current state."""
        return (
            self.state,
            self.tag_name,
            self.attr_name,
            self.quote,
            self._closing,
            self._raw,
            self._buf,
        )

    def restore(self, snapshot: Tuple[object, ...]) -> None:
        """Restore the tracker to a state previously captured with `snapshot()`."""
        (
            self.state,
            self.tag_name,
            self.attr_name,
            self.quote,
            self._closing,
            self._raw,
            self._buf,
        ) = snapshot

    def at_text(self) -> bool:
        """Return True if the tracker is at a clean HTML text position."""
        return self.state is _State.TEXT and not self._buf

    def output_kind(self) -> Optional[HTMLContextKind]:
        """Return the supported output context at the current position.

        Return `None` if the current position is not supported for dynamic
        output.
        """
        if self.state is _State.TEXT and not self._buf:
            return HTMLContextKind.TEXT
        if self.state is _State.ATTR_VALUE_QUOTED:
            if self.attr_name in URL_ATTRIBUTES:
                return HTMLContextKind.URL
            return HTMLContextKind.ATTR
        return None

    def describe(self) -> str:
        """Return a human readable description of the current position."""
        if self.state is _State.TEXT:
            if self._buf:
                return "HTML text, just after a dangling '<'"
            return "HTML text"
        if self.state is _State.TAG_NAME:
            return "a tag name"
        if self.state in (_State.TAG, _State.ATTR_NAME, _State.AFTER_ATTR_NAME):
            return f"tag <{self.tag_name}>, outside any attribute value"
        if self.state is _State.BEFORE_ATTR_VALUE:
            return f"before the value of attribute {self.attr_name!r}"
        if self.state is _State.ATTR_VALUE_UNQUOTED:
            return f"the unquoted value of attribute {self.attr_name!r}"
        if self.state is _State.ATTR_VALUE_QUOTED:
            return f"the quoted value of attribute {self.attr_name!r}"
        if self.state in (_State.RAWTEXT, _State.RCDATA):
            return f"<{self._raw}> content"
        if self.state is _State.COMMENT:
            return "an HTML comment"
        return "a markup declaration"

    def feed(self, text: str) -> None:
        """Feed literal template text to the tracker."""
        self._buf += text
        buf = self._buf
        i = 0
        n = len(buf)
        while i < n:
            state = self.state
            start = i
            if state is _State.TEXT:
                i = self._feed_text(buf, i, n)
            elif state is _State.TAG_NAME:
                i = self._feed_tag_name(buf, i, n)
            elif state is _State.TAG:
                i = self._feed_tag(buf, i, n)
            elif state is _State.ATTR_NAME:
                i = self._feed_attr_name(buf, i, n)
            elif state is _State.AFTER_ATTR_NAME:
                i = self._feed_after_attr_name(buf, i, n)
            elif state is _State.BEFORE_ATTR_VALUE:
                i = self._feed_before_attr_value(buf, i, n)
            elif state is _State.ATTR_VALUE_QUOTED:
                i = self._feed_quoted_value(buf, i, n)
            elif state is _State.ATTR_VALUE_UNQUOTED:
                i = self._feed_unquoted_value(buf, i, n)
            elif state is _State.COMMENT:
                i = self._feed_comment(buf, i, n)
            elif state is _State.DECL:
                i = self._feed_decl(buf, i, n)
            else:  # RAWTEXT or RCDATA
                i = self._feed_raw(buf, i, n)
            if i == start and self.state is state:
                break  # Undecidable input. Keep it buffered for the next feed.
        self._buf = buf[i:]

    def _end_tag(self) -> None:
        if not self._closing and self.tag_name in _RAWTEXT_TAGS:
            self.state = _State.RAWTEXT
            self._raw = self.tag_name
        elif not self._closing and self.tag_name in _RCDATA_TAGS:
            self.state = _State.RCDATA
            self._raw = self.tag_name
        else:
            self.state = _State.TEXT
        self.tag_name = ""
        self.attr_name = ""
        self.quote = ""
        self._closing = False

    def _feed_text(self, buf: str, i: int, n: int) -> int:
        j = buf.find("<", i)
        if j == -1:
            return n
        if j + 1 >= n:
            return j  # Keep the trailing "<" until we can decide.
        char = buf[j + 1]
        if _is_ascii_alpha(char):
            self.state = _State.TAG_NAME
            self.tag_name = char.lower()
            self._closing = False
            return j + 2
        if char == "/":
            if j + 2 >= n:
                return j  # Keep "</" until we can decide.
            char2 = buf[j + 2]
            if _is_ascii_alpha(char2):
                self.state = _State.TAG_NAME
                self.tag_name = char2.lower()
                self._closing = True
                return j + 3
            if char2 == ">":
                return j + 3  # "</>" is ignored entirely.
            self.state = _State.DECL  # Bogus comment.
            return j + 2
        if char == "!":
            if buf.startswith("<!--", j):
                self.state = _State.COMMENT
                return j + 4
            if j + 4 > n and "<!--".startswith(buf[j:]):
                return j  # Keep a partial "<!--" until we can decide.
            self.state = _State.DECL
            return j + 2
        if char == "?":
            self.state = _State.DECL  # Bogus comment.
            return j + 2
        return j + 1  # "<" is literal text.

    def _feed_tag_name(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _TAG_NAME_END:
                if char == ">":
                    self._end_tag()
                else:
                    self.state = _State.TAG
                return i + 1
            self.tag_name += char.lower()
            i += 1
        return n

    def _feed_tag(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _WHITESPACE:
                i += 1
            elif char == ">":
                self._end_tag()
                return i + 1
            elif char == "/":
                i += 1  # A self-closing flag does not cancel RAWTEXT/RCDATA.
            elif _is_ascii_alpha(char):
                self.state = _State.ATTR_NAME
                self.attr_name = char.lower()
                return i + 1
            else:
                i += 1  # Tolerate unexpected characters.
        return n

    def _feed_attr_name(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _ATTR_NAME_END:
                if char == "=":
                    self.state = _State.BEFORE_ATTR_VALUE
                elif char == ">":
                    self._end_tag()
                elif char == "/":
                    self.state = _State.TAG
                else:
                    self.state = _State.AFTER_ATTR_NAME
                return i + 1
            self.attr_name += char.lower()
            i += 1
        return n

    def _feed_after_attr_name(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _WHITESPACE:
                i += 1
            elif char == "=":
                self.state = _State.BEFORE_ATTR_VALUE
                return i + 1
            elif char == ">":
                self._end_tag()
                return i + 1
            elif char == "/":
                self.state = _State.TAG
                return i + 1
            elif _is_ascii_alpha(char):
                self.state = _State.ATTR_NAME
                self.attr_name = char.lower()
                return i + 1
            else:
                i += 1
        return n

    def _feed_before_attr_value(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _WHITESPACE:
                i += 1
            elif char in "\"'":
                self.state = _State.ATTR_VALUE_QUOTED
                self.quote = char
                return i + 1
            elif char == ">":
                self._end_tag()
                return i + 1
            else:
                self.state = _State.ATTR_VALUE_UNQUOTED
                return i
        return n

    def _feed_quoted_value(self, buf: str, i: int, n: int) -> int:
        j = buf.find(self.quote, i)
        if j == -1:
            return n
        self.state = _State.TAG
        return j + 1

    def _feed_unquoted_value(self, buf: str, i: int, n: int) -> int:
        while i < n:
            char = buf[i]
            if char in _WHITESPACE:
                self.state = _State.TAG
                return i + 1
            if char == ">":
                self._end_tag()
                return i + 1
            i += 1
        return n

    def _feed_comment(self, buf: str, i: int, n: int) -> int:
        j = buf.find("-->", i)
        if j == -1:
            # Keep a trailing partial "--" so a split "-->" is not missed.
            if buf.endswith("--"):
                return n - 2
            if buf.endswith("-"):
                return n - 1
            return n
        self.state = _State.TEXT
        return j + 3

    def _feed_decl(self, buf: str, i: int, n: int) -> int:
        j = buf.find(">", i)
        if j == -1:
            return n
        self.state = _State.TEXT
        return j + 1

    def _feed_raw(self, buf: str, i: int, n: int) -> int:
        # Search for a matching end tag, like "</script".
        needle = "</" + self._raw
        lower = buf.lower()
        j = lower.find(needle, i)
        while j != -1:
            k = j + len(needle)
            if k >= n:
                return j  # Keep a partial end tag until we can decide.
            if buf[k] in _TAG_NAME_END:
                end = buf.find(">", k)
                if end == -1:
                    return j  # Keep the end tag until its ">" arrives.
                self.state = _State.TEXT
                self._raw = ""
                return end + 1
            j = lower.find(needle, k)
        # Keep enough of a tail to catch an end tag split across feeds.
        return max(i, n - len(needle))


def escape_html_text(value: str) -> str:
    """Escape a string for output in HTML text context.

    Strings marked as "safe" (like `markupsafe.Markup` or objects implementing
    `__html__`) are returned unescaped. This is the only context in which safe
    strings are privileged.
    """
    return str(_escape_markup(value))


def escape_html_attribute(value: str) -> str:
    """Escape a string for output inside a quoted HTML attribute value.

    Safe string markers are not honored here. Everything is escaped, so a
    value can not close the attribute or the surrounding tag.
    """
    # Convert to a plain string. `markupsafe.Markup` overrides `str.replace`
    # to escape its arguments, which would double-escape here.
    value = str(value)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
        .replace("'", "&#39;")
    )


def _url_scheme(value: str) -> Optional[str]:
    value = str(value)
    normalized = value.strip(_URL_EDGE_STRIP)
    for char in _URL_EMBEDDED_STRIP:
        normalized = normalized.replace(char, "")
    match = _SCHEME_RE.match(normalized)
    return match.group(1).lower() if match else None


def is_safe_url(value: str) -> bool:
    """Return True if a URL attribute value uses an allowed protocol.

    The value is checked as-is and after one level of HTML entity decoding, so
    whitespace and entity obfuscation (like `java&#115;cript:`) can not hide a
    disallowed protocol.
    """
    for candidate in (value, unescape(value)):
        scheme = _url_scheme(candidate)
        if scheme is not None and scheme not in ALLOWED_URL_SCHEMES:
            return False
    return True


def escape_for_context(
    value: str, kind: HTMLContextKind, *, token: Optional[Token] = None
) -> str:
    """Escape a stringified value for the given HTML context.

    Raise `HTMLContextError` if a URL attribute value uses a disallowed
    protocol.
    """
    if kind is HTMLContextKind.TEXT:
        return escape_html_text(value)
    if kind is HTMLContextKind.URL and not is_safe_url(value):
        raise HTMLContextError(
            f"disallowed URL protocol in {value[:32]!r}", token=token
        )
    return escape_html_attribute(value)
