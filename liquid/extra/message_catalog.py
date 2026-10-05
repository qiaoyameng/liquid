"""Message catalogs for the optional _message_ tag.

The :class:`~liquid.extra.message_tag.MessageTag` tag does not do any
translation itself. Applications provide a *message catalog*, keyed by
message ID, locale and [Babel plural category]_.

.. [Babel plural category]
   https://babel.pocoo.org/en/latest/api/plural.html
"""

from __future__ import annotations

import re
from decimal import Decimal
from decimal import InvalidOperation
from functools import lru_cache
from typing import Mapping
from typing import Optional
from typing import Union

from babel import Locale
from babel import UnknownLocaleError
from babel.plural import PluralRule

from liquid import Markup
from liquid.stringify import to_liquid_string

__all__ = (
    "MessageCatalog",
    "MessageCatalogError",
    "PluralMessage",
    "check_catalog",
    "fallback_chain",
    "format_message",
    "plural_categories",
    "plural_category",
    "validate_fallback_chain",
)

#: A single, non-plural message.
SimpleMessage = str

#: A plural message maps Babel plural category names to message text.
PluralMessage = Mapping[str, str]

#: Messages for a single locale, keyed by message ID.
LocaleMessages = Mapping[str, Union[str, Mapping[str, str]]]

#: A message catalog keyed by locale, then message ID.
MessageCatalog = Mapping[str, LocaleMessages]

#: Maps a locale to the locales that should be tried when it does not
#: contain a translation.
FallbackChain = Mapping[str, list[str]]

#: The plural categories defined by CLDR/Babel.
CATEGORIES: frozenset[str] = frozenset(
    ("zero", "one", "two", "few", "many", "other")
)

#: A number accepted as a pluralization count. Integers, floats and
#: :class:`decimal.Decimal` are passed straight through to Babel. Strings are
#: converted to :class:`decimal.Decimal` so the visible fractional digits are
#: preserved and the value is never rounded to an integer first.
Count = Union[int, float, Decimal, str]

# Match `{name}` placeholders and `{{`/`}}` escaped braces. Anything else that
# looks brace-like is rejected so that unsupported syntax can never silently
# reach the output or be re-interpreted by a template engine.
_re_placeholder = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
_re_brace = re.compile(r"[{}]")


class MessageCatalogError(ValueError):
    """Raised when a message catalog or fallback chain is invalid."""


@lru_cache(maxsize=None)
def _locale(tag: str) -> Locale:
    try:
        return Locale.parse(tag)
    except (UnknownLocaleError, ValueError) as err:
        raise MessageCatalogError(f"unknown locale {tag!r}") from err


@lru_cache(maxsize=None)
def plural_categories(locale_tag: str) -> tuple[str, ...]:
    """Return the plural categories used by _locale_tag_.

    `"other"` is always included, even though Babel does not list it.
    """
    rule = _locale(locale_tag).plural_form
    assert isinstance(rule, PluralRule)
    return tuple(sorted(rule.tags | {"other"}))


@lru_cache(maxsize=None)
def plural_rule(locale_tag: str) -> PluralRule:
    """Return Babel's plural rule for _locale_tag_."""
    rule = _locale(locale_tag).plural_form
    assert isinstance(rule, PluralRule)
    return rule


def plural_category(locale_tag: str, count: object) -> str:
    """Return the Babel plural category for _count_ in _locale_tag_.

    `Decimal`, `int` and `float` values are handed to Babel untouched. Strings
    are parsed with :class:`decimal.Decimal`, preserving trailing zeros and
    fractional digits. Values are never rounded to an integer before the
    plural category is selected.
    """
    number = _as_number(count)
    return plural_rule(locale_tag)(number)


def _as_number(count: object) -> Union[int, float, Decimal]:
    if isinstance(count, bool):
        raise MessageCatalogError(f"count must be a number, found {count!r}")
    if isinstance(count, (int, float, Decimal)):
        return count
    if isinstance(count, str):
        try:
            return Decimal(count)
        except InvalidOperation as err:
            raise MessageCatalogError(f"count must be a number, found {count!r}") from err
    raise MessageCatalogError(f"count must be a number, found {count!r}")


def validate_fallback_chain(chain: FallbackChain) -> None:
    """Raise :class:`MessageCatalogError` if _chain_ contains a cycle.

    Every value must be a list of locale tags. Self loops and chains that
    eventually lead back to themselves are rejected.
    """
    if not isinstance(chain, Mapping):
        raise MessageCatalogError("fallback chain must be a mapping of locale tags")

    for locale_tag, fallbacks in chain.items():
        if isinstance(fallbacks, str) or not all(
            isinstance(fallback, str) for fallback in fallbacks
        ):
            raise MessageCatalogError(
                f"fallbacks for locale {locale_tag!r} must be a list of locale tags"
            )

    def _detect_cycle(node: str, stack: list[str]) -> None:
        if node in stack:
            cycle = " -> ".join(stack[stack.index(node) :] + [node])
            raise MessageCatalogError(
                f"cycle detected in fallback chain: {cycle}"
            )
        stack.append(node)
        for fallback in chain.get(node, ()):  # type: ignore[union-attr]
            _detect_cycle(fallback, stack)
        stack.pop()

    for start in chain:
        _detect_cycle(start, [])


def fallback_chain(
    locale_tag: str,
    chain: Optional[FallbackChain] = None,
    *,
    default_locale: Optional[str] = None,
) -> list[str]:
    """Return the locales to try for _locale_tag_, in priority order.

    The requested locale comes first, followed by its configured fallbacks
    (transitively, with duplicates removed). If _default_locale_ is given and
    not already included, it is appended last.
    """
    chain = chain or {}
    locales: list[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        if tag not in seen:
            seen.add(tag)
            locales.append(tag)
            for fallback in chain.get(tag, ()):  # type: ignore[union-attr]
                _add(fallback)

    _add(locale_tag)

    if default_locale:
        _add(default_locale)

    return locales


def placeholder_names(text: str) -> tuple[str, ...]:
    """Return the named placeholders found in message _text_.

    Raises :class:`MessageCatalogError` if the text contains positional
    placeholders, format specifiers, attribute/index access or any other
    unsupported brace syntax.
    """
    names: list[str] = []
    pos = 0
    for match in _re_placeholder.finditer(text):
        if _re_brace.search(text[pos:match.start()]):
            raise MessageCatalogError(
                f"unsupported placeholder syntax in message {text!r}"
            )
        pos = match.end()
        name = match.group(1)
        if name is not None and name not in names:
            names.append(name)

    if _re_brace.search(text[pos:]):
        raise MessageCatalogError(f"unsupported placeholder syntax in message {text!r}")

    return tuple(names)


def check_catalog(
    catalog: MessageCatalog,
    *,
    fallback_chain_map: Optional[FallbackChain] = None,
    default_locale: Optional[str] = None,
    message_ids: Optional[list[str]] = None,
) -> None:
    """Validate _catalog_ and raise :class:`MessageCatalogError` on problems.

    Checks performed:

    * Every fallback chain entry must resolve to a locale in the catalog
      unless it is itself a known Babel locale.
    * Entries must be either strings or plural category mappings, and all
      locales must agree on the shape of a given message ID.
    * Plural messages must include every plural category the locale uses.
    * Every category key must be a valid Babel plural category.
    * Placeholder syntax must be valid and the set of placeholder names must
      be identical across all categories and locales for a message ID.
    * Every ID in _message_ids_ must be translated for _default_locale_ (or
      every locale if no default is configured).
    """
    if not isinstance(catalog, Mapping):
        raise MessageCatalogError("message catalog must be a mapping of locale tags")

    validate_fallback_chain(fallback_chain_map or {})

    for locale_tag, fallbacks in (fallback_chain_map or {}).items():
        if locale_tag not in catalog:
            continue
        for fallback in fallbacks:
            if fallback not in catalog:
                try:
                    Locale.parse(fallback)
                except UnknownLocaleError:
                    raise MessageCatalogError(
                        f"unknown locale {fallback!r} in fallback chain "
                        f"for {locale_tag!r}"
                    ) from None

    if default_locale is not None and default_locale not in catalog:
        raise MessageCatalogError(
            f"default locale {default_locale!r} is missing from the message catalog"
        )

    # Validate catalog structure and remember the shape/placeholders seen for
    # each message ID so they can be compared across locales.
    expected_shape: dict[str, type[object]] = {}
    expected_placeholders: dict[str, frozenset[str]] = {}

    for locale_tag, messages in catalog.items():
        if not isinstance(messages, Mapping):
            raise MessageCatalogError(
                f"messages for locale {locale_tag!r} must be a mapping"
            )

        # Every locale key must be a locale Babel can resolve.
        plural_categories(locale_tag)

        for message_id, message in messages.items():
            names = validate_message(message, locale_tag, message_id)
            shape: type[object] = (
                Mapping if isinstance(message, Mapping) else str
            )

            previous_shape = expected_shape.get(message_id)
            if previous_shape is not None and previous_shape is not shape:
                raise MessageCatalogError(
                    f"message {message_id!r} mixes plural and non-plural forms"
                )
            expected_shape[message_id] = shape

            previous_names = expected_placeholders.get(message_id)
            if previous_names is not None and previous_names != names:
                raise MessageCatalogError(
                    f"placeholder mismatch for message {message_id!r}: "
                    f"expected {sorted(previous_names)}, found {sorted(names)}"
                )
            else:
                expected_placeholders[message_id] = names

    if message_ids:
        check_locales = (
            [default_locale] if default_locale is not None else list(catalog)
        )
        for message_id in message_ids:
            for locale_tag in check_locales:
                messages = catalog.get(locale_tag, {})
                if message_id not in messages:
                    raise MessageCatalogError(
                        f"message {message_id!r} is missing from locale "
                        f"{locale_tag!r}"
                    )


def resolve_message(
    catalog: MessageCatalog,
    message_id: str,
    locale_tag: str,
    *,
    fallback_chain_map: Optional[FallbackChain] = None,
    default_locale: Optional[str] = None,
    count: object = 0,
) -> tuple[str, str]:
    """Look up _message_id_ and return `(resolved_locale, message_text)`.

    Locales are tried in the order returned by :func:`fallback_chain`. If a
    locale contains the message ID but is missing the plural category
    selected for `count`, that is an error rather than silently falling back.
    """
    number = _as_number(count) if count is not None else Decimal(0)
    locales = fallback_chain(
        locale_tag, fallback_chain_map, default_locale=default_locale
    )

    for candidate in locales:
        messages = catalog.get(candidate)
        if messages is None:
            continue
        message = messages.get(message_id)
        if message is None:
            continue

        validate_message(message, candidate, message_id)

        if isinstance(message, Mapping):
            category = plural_rule(candidate)(number)
            text = message.get(category)
            if text is None:
                raise MessageCatalogError(
                    f"message {message_id!r} in locale {candidate!r} is missing "
                    f"plural category {category!r}"
                )
            return candidate, text
        return candidate, message

    chain = " -> ".join(locales)
    raise MessageCatalogError(
        f"message {message_id!r} is not available for locale {locale_tag!r} "
        f"(tried {chain})"
    )


def validate_message(
    message: Union[str, Mapping[str, str]],
    locale_tag: str,
    message_id: str,
) -> frozenset[str]:
    """Validate a catalog entry and return its placeholder names."""
    if isinstance(message, str):
        return frozenset(placeholder_names(message))

    if not isinstance(message, Mapping):
        raise MessageCatalogError(
            f"message {message_id!r} in locale {locale_tag!r} must be a string "
            "or plural category mapping"
        )

    unknown = set(message) - CATEGORIES
    if unknown:
        raise MessageCatalogError(
            f"unknown plural category(s) {sorted(unknown)} for message "
            f"{message_id!r} in locale {locale_tag!r}"
        )

    missing = set(plural_categories(locale_tag)) - set(message)
    if missing:
        raise MessageCatalogError(
            f"message {message_id!r} in locale {locale_tag!r} is missing plural "
            f"category(s) {sorted(missing)}"
        )

        names: Optional[frozenset[str]] = None
        for category, text in message.items():
            if not isinstance(text, str):
                raise MessageCatalogError(
                    f"message {message_id!r} category {category!r} in locale "
                    f"{locale_tag!r} must be a string"
                )
            category_names = frozenset(placeholder_names(text))
            if names is None:
                names = category_names
            elif category_names != names:
                raise MessageCatalogError(
                    f"placeholder mismatch in plural categories for message "
                    f"{message_id!r} in locale {locale_tag!r}"
                )
        assert names is not None
        return names


def format_message(
    text: str,
    values: Mapping[str, object],
    *,
    autoescape: bool = False,
) -> str:
    """Replace named placeholders in _text_ with _values_.

    Only named placeholders (`{name}`) are substituted; the message text is
    never parsed as a template. Raises :class:`MessageCatalogError` for
    missing or extra values. When _autoescape_ is true, values are HTML
    escaped while literal message text is left untouched.
    """
    names = placeholder_names(text)

    missing = [name for name in names if name not in values]
    if missing:
        raise MessageCatalogError(
            f"missing placeholder value(s) {missing} for message {text!r}"
        )

    extra = sorted(set(values) - set(names))
    if extra:
        raise MessageCatalogError(
            f"unexpected placeholder value(s) {extra} for message {text!r}"
        )

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        return to_liquid_string(values[name], autoescape=autoescape)

    rendered = _re_placeholder.sub(_replace, text)
    if autoescape:
        return Markup(rendered)
    return rendered
