"""Context-specific escaping and URL sanitization.

Three dynamic positions are supported by the first version of the contextual
escaping mode:

* HTML text content (element data)
* Quoted attribute values
* Values of designated URL-bearing attributes (still written into a quoted
  attribute value, but with an additional protocol policy check)
"""

from __future__ import annotations

import html as _html
import re
from typing import Any
from typing import Optional
from typing import TYPE_CHECKING

from markupsafe import Markup
from markupsafe import escape as _markupsafe_escape
from markupsafe import soft_str

from .exceptions import ContextualEscapeError

if TYPE_CHECKING:
    from liquid.token import Token

#: URL schemes allowed in URL-bearing attribute values.
DEFAULT_ALLOWED_URL_SCHEMES = frozenset(
    ("http", "https", "mailto", "tel", "ftp", "ftps")
)

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")

# Code points that browsers may ignore while resolving a URL, used to uncover
# obfuscated schemes such as "java\x00script:" or bidi/soft-hyphen tricks.
_PREEMPTABLE = frozenset(
    (
        "\x00",
        "\x01",
        "\x02",
        "\x03",
        "\x04",
        "\x05",
        "\x06",
        "\x07",
        "\x08",
        "\x09",
        "\x0a",
        "\x0b",
        "\x0c",
        "\x0d",
        "\x0e",
        "\x0f",
        "\x10",
        "\x11",
        "\x12",
        "\x13",
        "\x14",
        "\x15",
        "\x16",
        "\x17",
        "\x18",
        "\x19",
        "\x1a",
        "\x1b",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x1f",
        " ",
        "\x7f",
        "\xad",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\ufffd",
    )
)


class SafeFragment(Markup):
    """A string produced by the template itself, such as a captured block.

    It may be emitted without escaping in HTML text context, where it consists
    of already-escaped output, but receives no special privilege in attribute
    or URL contexts.
    """


def contextual_to_liquid_string(val: Any) -> tuple[str, bool]:
    """Stringify a Liquid value for contextual escaping.

    Returns the string representation and a flag indicating if the value is
    already marked safe (either explicitly or via ``__html__``).
    """
    if isinstance(val, Markup):
        return str(val), True

    if isinstance(val, str):
        return val, False

    if hasattr(val, "__html__"):
        try:
            return str(val.__html__()), True
        except Exception:  # noqa: BLE001
            return str(val), False

    if isinstance(val, bool):
        return str(val).lower(), False

    if val is None:
        return "", False

    if isinstance(val, list):
        return (
            "".join(contextual_to_liquid_string(item)[0] for item in val),
            False,
        )

    if isinstance(val, range):
        return f"{val.start}..{val.stop - 1}", False

    return soft_str(val), False


def escape_text(val: str) -> str:
    """Escape a dynamic value for the HTML text context."""
    return str(_markupsafe_escape(val))


def escape_attribute(val: str) -> str:
    """Escape a dynamic value for a quoted attribute value context."""
    return str(_markupsafe_escape(val))


def _clean_for_scheme(value: str) -> str:
    # Remove control characters, spaces and other ignorable code points so
    # that "java\x09script:..." can not slip past scheme detection.
    return "".join(ch for ch in value if ch not in _PREEMPTABLE)


def _normalize_parts(parts: list[tuple[str, bool]]) -> str:
    """Combine URL attribute value parts into one raw, entity-decoded URL.

    Literal parts come from template authors and may contain HTML entities;
    dynamic parts are rendered already escaped, so entity references found
    there are literal text and must not be decoded.
    """
    chunks: list[str] = []
    for text, is_literal in parts:
        if is_literal:
            chunks.append(_html.unescape(text))
        else:
            chunks.append(text)
    return "".join(chunks)


def check_url(
    raw: str,
    *,
    allowed_url_schemes: Optional[frozenset[str]] = None,
    token: Optional["Token"] = None,
) -> None:
    """Raise if _raw_ uses an unsafe URL protocol.

    The value is expected to be the concatenation of (entity decoded) literal
    template text and raw dynamic values, as a browser would see it after
    entity decoding.
    """
    schemes = allowed_url_schemes or DEFAULT_ALLOWED_URL_SCHEMES
    cleaned = _clean_for_scheme(raw)

    if not cleaned:
        return

    # Relative fragments and queries are always fine.
    if cleaned[0] in ("#", "?", "/"):
        return

    # A leading backslash is treated like a slash by many parsers.
    if cleaned.startswith("\\"):
        return

    match = _SCHEME_RE.match(cleaned)
    if match is not None:
        scheme = match.group(1).lower()
        if scheme not in schemes:
            raise ContextualEscapeError(
                f"URL protocol '{scheme}:' is not allowed in this context",
                token=token,
            )


def validate_url_parts(
    parts: list[tuple[str, bool]],
    *,
    allowed_url_schemes: Optional[frozenset[str]] = None,
    token: Optional["Token"] = None,
) -> None:
    """Validate the protocol of a fully assembled URL attribute value."""
    check_url(
        _normalize_parts(parts),
        allowed_url_schemes=allowed_url_schemes,
        token=token,
    )
