"""Opt-in, context aware HTML escaping for Liquid templates.

Enable with `liquid.Environment(contextual_escape=True)`. The first version
supports dynamic values in three positions:

* HTML text content
* Quoted attribute values
* Values of designated URL-bearing attributes (`href`, `src`, ...)

All other dynamic positions (tag names, attribute names, unquoted attribute
values, ``<script>``/``<style>`` content) are rejected, either at parse time
by the static validator or when reached while rendering.
"""

from __future__ import annotations

from .context import HtmlState
from .exceptions import ContextualEscapeError
from .escape import DEFAULT_ALLOWED_URL_SCHEMES
from .escape import SafeFragment
from .escape import check_url
from .escape import escape_attribute
from .escape import escape_text
from .validate import TemplateValidator

__all__ = (
    "DEFAULT_ALLOWED_URL_SCHEMES",
    "ContextualEscapeError",
    "HtmlState",
    "SafeFragment",
    "TemplateValidator",
    "check_url",
    "escape_attribute",
    "escape_text",
)
