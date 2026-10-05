"""A small HTML context tracking state machine.

Used by the static validator (to prove every branch, captured block and
included snippet leaves the parser in the same structural state) and at render
time to choose the correct escape policy for dynamic values.

Only static template text advances the machine. Dynamic output statements
(``{{ ... }}``) are treated as opaque values whose escaping policy is chosen
from the machine state at the point they appear.
"""

from __future__ import annotations

import sys
from typing import Any
from typing import Optional
from typing import Tuple

from . import escape as esc
from .exceptions import ContextualEscapeError

# Parser states
STATE_DATA = sys.intern("data")
STATE_RAWTEXT = sys.intern("rawtext")
STATE_RCDATA = sys.intern("rcdata")
STATE_TAG_OPEN = sys.intern("tag_open")
STATE_END_TAG_OPEN = sys.intern("end_tag_open")
STATE_TAG_NAME = sys.intern("tag_name")
STATE_END_TAG_NAME = sys.intern("end_tag_name")
STATE_BEFORE_ATTR = sys.intern("before_attr")
STATE_ATTR_NAME = sys.intern("attr_name")
STATE_AFTER_ATTR_NAME = sys.intern("after_attr_name")
STATE_BEFORE_ATTR_VALUE = sys.intern("before_attr_value")
STATE_ATTR_VALUE_DQ = sys.intern("attr_value_dq")
STATE_ATTR_VALUE_SQ = sys.intern("attr_value_sq")
STATE_SELF_CLOSING = sys.intern("self_closing")
STATE_COMMENT = sys.intern("comment")
STATE_COMMENT_DASH = sys.intern("comment_dash")
STATE_COMMENT_END = sys.intern("comment_end")
STATE_BOGUS_COMMENT = sys.intern("bogus_comment")
STATE_DECLARATION = sys.intern("declaration")

# Elements whose contents are raw text. Dynamic data is never allowed there.
RAWTEXT_TAGS = frozenset(
    ("script", "style", "xmp", "iframe", "noembed", "noframes", "noscript")
)

# Elements whose contents are RCDATA. Dynamic data is allowed and escaped as
# text (entities are interpreted, but no markup is recognized inside).
RCDATA_TAGS = frozenset(("title", "textarea"))

# Attributes whose value is interpreted as a URL.
URL_ATTRIBUTES = frozenset(
    (
        "action",
        "archive",
        "background",
        "cite",
        "classid",
        "codebase",
        "data",
        "formaction",
        "href",
        "icon",
        "longdesc",
        "manifest",
        "poster",
        "profile",
        "src",
        "usemap",
        "xlink:href",
        "xmlns",
    )
)

HtmlSignature = Tuple[str, str, str, str]
"""Policy relevant insertion context: (state, tag, attribute, quote)."""


class HtmlState:
    """Mutable HTML parser context."""

    __slots__ = (
        "state",
        "tag_name",
        "attr_name",
        "quote",
        "url_parts",
        "raw_pending",
        "allowed_url_schemes",
    )

    def __init__(
        self,
        *,
        state: str = STATE_DATA,
        tag_name: str = "",
        attr_name: str = "",
        quote: str = "",
        url_parts: Optional[list] = None,
        raw_pending: str = "",
        allowed_url_schemes: Optional[frozenset] = None,
    ) -> None:
        self.state = state
        self.tag_name = tag_name
        self.attr_name = attr_name
        self.quote = quote
        self.url_parts = url_parts if url_parts is not None else []
        self.raw_pending = raw_pending
        self.allowed_url_schemes = (
            allowed_url_schemes or esc.DEFAULT_ALLOWED_URL_SCHEMES
        )

    def clone(self) -> "HtmlState":
        return HtmlState(
            state=self.state,
            tag_name=self.tag_name,
            attr_name=self.attr_name,
            quote=self.quote,
            url_parts=list(self.url_parts),
            raw_pending=self.raw_pending,
            allowed_url_schemes=self.allowed_url_schemes,
        )

    def copy_from(self, other: "HtmlState") -> None:
        """Copy the parser state from _other_ into this state."""
        self.state = other.state
        self.tag_name = other.tag_name
        self.attr_name = other.attr_name
        self.quote = other.quote
        self.url_parts = list(other.url_parts)
        self.raw_pending = other.raw_pending
        self.allowed_url_schemes = other.allowed_url_schemes

    def signature(self) -> HtmlSignature:
        return (self.state, self.tag_name, self.attr_name, self.quote)

    def policy_signature(self) -> tuple:
        """Return a signature of only the escape-policy-relevant state.

        The most recently closed tag name is not policy relevant once we are
        back in element data context, so branches that close different tags
        remain compatible.
        """
        return (self.state, self.attr_name, self.quote)

    def is_url_attr(self) -> bool:
        name = self.attr_name.lower()
        if name in URL_ATTRIBUTES:
            return True
        if ":" in name:
            return name.split(":", 1)[1] in URL_ATTRIBUTES
        return False

    # -- static template text --------------------------------------------

    def feed_literal(self, text: str, token: Any = None) -> None:
        i = 0
        n = len(text)
        while i < n:
            if self.state in (STATE_RAWTEXT, STATE_RCDATA):
                i = self._advance_rawtext(text, i)
                continue

            ch = text[i]
            state = self.state

            if state == STATE_DATA:
                if ch == "<":
                    self.state = STATE_TAG_OPEN
                i += 1

            elif state == STATE_TAG_OPEN:
                if ch == "!":
                    i = self._open_markup(text, i, token)
                elif ch == "/":
                    self.state = STATE_END_TAG_OPEN
                    i += 1
                elif ch.isalpha():
                    self.tag_name = ch.lower()
                    self.attr_name = ""
                    self.quote = ""
                    self.state = STATE_TAG_NAME
                    i += 1
                else:
                    # A stray "<" is literal text.
                    self.state = STATE_DATA
                    i += 1

            elif state == STATE_END_TAG_OPEN:
                if ch.isalpha():
                    self.tag_name = ch.lower()
                    self.state = STATE_END_TAG_NAME
                    i += 1
                elif ch == ">":
                    self.state = STATE_DATA
                    i += 1
                else:
                    self.state = STATE_BOGUS_COMMENT
                    i += 1

            elif state == STATE_TAG_NAME:
                if ch.isspace():
                    self.state = STATE_BEFORE_ATTR
                elif ch == ">":
                    self._after_open_tag()
                elif ch == "/":
                    self.state = STATE_BEFORE_ATTR
                else:
                    self.tag_name += ch.lower()
                i += 1

            elif state == STATE_END_TAG_NAME:
                if ch.isspace():
                    self.state = STATE_BOGUS_COMMENT
                elif ch == ">":
                    self._close_tag()
                elif ch == "/":
                    self.state = STATE_BOGUS_COMMENT
                else:
                    self.tag_name += ch.lower()
                i += 1

            elif state == STATE_BEFORE_ATTR:
                if ch.isspace():
                    i += 1
                elif ch == ">":
                    self._after_open_tag()
                elif ch == "/":
                    self.state = STATE_SELF_CLOSING
                else:
                    self.attr_name = ch.lower()
                    self.state = STATE_ATTR_NAME
                i += 1

            elif state == STATE_ATTR_NAME:
                if ch.isspace():
                    self.state = STATE_AFTER_ATTR_NAME
                elif ch == "=":
                    self.state = STATE_BEFORE_ATTR_VALUE
                elif ch == ">":
                    self._after_open_tag()
                elif ch == "/":
                    self.state = STATE_SELF_CLOSING
                else:
                    self.attr_name += ch.lower()
                i += 1

            elif state == STATE_AFTER_ATTR_NAME:
                if ch.isspace():
                    i += 1
                elif ch == "=":
                    self.state = STATE_BEFORE_ATTR_VALUE
                elif ch == ">":
                    self._after_open_tag()
                elif ch == "/":
                    self.state = STATE_SELF_CLOSING
                else:
                    self.attr_name = ch.lower()
                    self.state = STATE_ATTR_NAME
                    i += 1

            elif state == STATE_SELF_CLOSING:
                if ch == ">":
                    self._after_open_tag()
                elif ch.isspace():
                    pass
                else:
                    # Not actually self-closing; parse as another attribute.
                    self.state = STATE_ATTR_NAME
                    self.attr_name = ch.lower()
                i += 1

            elif state == STATE_BEFORE_ATTR_VALUE:
                if ch.isspace():
                    i += 1
                elif ch == '"':
                    self.quote = '"'
                    self.url_parts = []
                    self.state = STATE_ATTR_VALUE_DQ
                    i += 1
                elif ch == "'":
                    self.quote = "'"
                    self.url_parts = []
                    self.state = STATE_ATTR_VALUE_SQ
                    i += 1
                elif ch == ">":
                    self._after_open_tag()
                else:
                    raise ContextualEscapeError(
                        "unquoted attribute values are not supported by the "
                        "contextual escaping mode",
                        token=token,
                    )

            elif state == STATE_ATTR_VALUE_DQ:
                if ch == '"':
                    self._close_attr_value(token)
                else:
                    self._append_literal_part(ch)
                i += 1

            elif state == STATE_ATTR_VALUE_SQ:
                if ch == "'":
                    self._close_attr_value(token)
                else:
                    self._append_literal_part(ch)
                i += 1

            elif state == STATE_COMMENT:
                if ch == "-":
                    self.state = STATE_COMMENT_DASH
                i += 1

            elif state == STATE_COMMENT_DASH:
                if ch == "-":
                    self.state = STATE_COMMENT_END
                else:
                    self.state = STATE_COMMENT
                i += 1

            elif state == STATE_COMMENT_END:
                if ch == ">":
                    self.state = STATE_DATA
                elif ch != "-":
                    self.state = STATE_COMMENT
                i += 1

            elif state == STATE_BOGUS_COMMENT:
                if ch == ">":
                    self.state = STATE_DATA
                i += 1

            elif state == STATE_DECLARATION:
                if ch == ">":
                    self.state = STATE_DATA
                i += 1

            else:  # pragma: no cover
                i += 1

    def _open_markup(self, text: str, start: int, token: Any) -> int:
        rest = text[start + 1:]
        if rest.startswith("--"):
            self.state = STATE_COMMENT
            return start + 3
        if rest.upper().startswith("[CDATA["):
            self.state = STATE_BOGUS_COMMENT
            return start + 8
        self.state = STATE_DECLARATION
        return start + 1

    def _advance_rawtext(self, text: str, start: int) -> int:
        pending_len = len(self.raw_pending)
        data = self.raw_pending + text[start:]
        marker = "</" + self.tag_name
        low = data.lower()
        search_from = 0

        while True:
            idx = low.find(marker, search_from)
            if idx == -1:
                # Retain a possible partial marker at the very end so it can be
                # found when the next chunk arrives.
                keep = min(len(marker) - 1, len(data))
                self.raw_pending = data[len(data) - keep:]
                return len(text)

            after = idx + len(marker)
            if after < len(data) and not (
                data[after].isspace() or data[after] in ">/"
            ):
                search_from = idx + 1
                continue

            self.raw_pending = ""
            self.state = STATE_END_TAG_NAME
            pos = start + after - pending_len
            if pos < start:
                pos = start
            return pos

    def _close_attr_value(self, token: Any) -> None:
        if self.is_url_attr():
            esc.validate_url_parts(
                self.url_parts,
                allowed_url_schemes=self.allowed_url_schemes,
                token=token,
            )
        self.url_parts = []
        self.quote = ""
        self.state = STATE_BEFORE_ATTR

    def _append_literal_part(self, text: str) -> None:
        if self.url_parts and self.url_parts[-1][1] is True:
            prev = self.url_parts[-1][0]
            self.url_parts[-1] = (prev + text, True)
        else:
            self.url_parts.append((text, True))

    def _after_open_tag(self) -> None:
        name = self.tag_name
        self.attr_name = ""
        self.quote = ""
        self.url_parts = []
        if name in RAWTEXT_TAGS:
            self.state = STATE_RAWTEXT
        elif name in RCDATA_TAGS:
            self.state = STATE_RCDATA
        else:
            self.state = STATE_DATA

    def _close_tag(self) -> None:
        self.attr_name = ""
        self.url_parts = []
        self.state = STATE_DATA

    # -- dynamic values ---------------------------------------------------

    def dynamic_policy(self, token: Any = None) -> str:
        state = self.state
        if state == STATE_DATA or state == STATE_RCDATA:
            return "text"
        if state == STATE_ATTR_VALUE_DQ or state == STATE_ATTR_VALUE_SQ:
            return "url" if self.is_url_attr() else "attr"
        if state == STATE_RAWTEXT:
            raise ContextualEscapeError(
                f"dynamic values are not allowed inside <{self.tag_name}> "
                "elements",
                token=token,
            )
        raise ContextualEscapeError(
            "dynamic values are only allowed in HTML text content and quoted "
            "attribute values",
            token=token,
        )

    def finish(self, token: Any = None) -> None:
        if self.state != STATE_DATA:
            raise ContextualEscapeError(
                f"template ends inside an HTML {self._describe()}",
                token=token,
            )

    def _describe(self) -> str:
        state = self.state
        if state in (STATE_ATTR_VALUE_DQ, STATE_ATTR_VALUE_SQ):
            q = "double" if state == STATE_ATTR_VALUE_DQ else "single"
            return (
                f"quoted attribute value ({q} quotes), attribute "
                f"{self.attr_name!r}"
            )
        if state == STATE_ATTR_NAME or state == STATE_AFTER_ATTR_NAME:
            return "attribute name"
        if state == STATE_BEFORE_ATTR_VALUE:
            return "unquoted or missing attribute value"
        if state in (STATE_BEFORE_ATTR, STATE_TAG_OPEN, STATE_END_TAG_OPEN,
                     STATE_TAG_NAME, STATE_END_TAG_NAME):
            return "tag"
        if state in (STATE_RAWTEXT, STATE_RCDATA):
            return f"<{self.tag_name}> element"
        if state in (STATE_COMMENT, STATE_COMMENT_DASH, STATE_COMMENT_END,
                     STATE_BOGUS_COMMENT, STATE_DECLARATION):
            return "comment or declaration"
        return state
