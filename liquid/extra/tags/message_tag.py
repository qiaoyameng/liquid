"""An optional ``message`` tag for localized, plural-aware messages.

Messages are looked up by message ID in an application-supplied
:class:`MessageCatalog`, using Babel's plural rules to select a plural form
for the message's locale and count. Message text is never parsed or rendered
as a Liquid template. Only named placeholders of the form ``%(name)s`` are
supported, and placeholder values are escaped according to the environment's
autoescape setting.
"""

from __future__ import annotations

import re
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import TextIO
from typing import Union

from babel import Locale
from babel import UnknownLocaleError

from liquid.ast import Node
from liquid.builtin.expressions import Identifier
from liquid.builtin.expressions import KeywordArgument
from liquid.builtin.expressions import Path
from liquid.builtin.expressions import StringLiteral
from liquid.builtin.expressions import parse_primitive
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import TranslationError
from liquid.stringify import to_liquid_string
from liquid.tag import Tag
from liquid.token import TOKEN_TAG

if TYPE_CHECKING:
    from liquid import Expression
    from liquid import RenderContext
    from liquid import TokenStream
    from liquid.token import Token

# A mapping of message ID to locale to plural category to message text, or,
# for messages without plural forms, message ID to locale to message text.
MessageData = Mapping[str, Mapping[str, Union[str, Mapping[str, str]]]]

RE_PLACEHOLDER = re.compile(r"(?<!%)%\((\w+)\)s")
RE_INTERPOLATE = re.compile(r"%%|%\((\w+)\)s")
RE_MALFORMED_PLACEHOLDER = re.compile(r"%\(")


class MessageError(TranslationError):
    """Base class for errors raised by the ``message`` tag."""

    def __init__(self, *args: object, token: Optional[Token] = None):
        super().__init__(*args, token=token)


class MessageCatalogError(MessageError):
    """Raised when a message catalog or its configuration is invalid."""


class MessageLookupError(MessageError):
    """Raised when a message, locale or plural form can not be found."""


class MessageInterpolationError(MessageError):
    """Raised when placeholder values are missing, unexpected or invalid."""


class MessageCatalog:
    """A message catalog mapping message IDs to localized, pluralized text.

    Arguments:
        messages: A mapping of message ID to locale to message text, where
            message text is either a string or a mapping of CLDR plural
            category (``"zero"``, ``"one"``, ``"two"``, ``"few"``, ``"many"``
            or ``"other"``) to message text. A plain string is equivalent to
            a mapping with a single ``"other"`` category.
        default_locale: The locale used when a ``message`` tag does not
            specify one and no ``locale`` variable is in scope.
        fallbacks: A mapping of locale to a sequence of fallback locales.
            When a message is not available for a locale, its fallbacks are
            tried in order. Fallbacks are transitive. Cycles are rejected.

    Message text may contain named placeholders of the form ``%(name)s``.
    Use ``%%`` for a literal percent sign.
    """

    def __init__(
        self,
        messages: MessageData,
        *,
        default_locale: str = "en",
        fallbacks: Optional[Mapping[str, Sequence[str]]] = None,
    ):
        self.messages: dict[str, dict[str, dict[str, str]]] = {
            message_id: {
                locale: ({"other": text} if isinstance(text, str) else dict(text))
                for locale, text in locales.items()
            }
            for message_id, locales in messages.items()
        }
        self.default_locale = default_locale
        self.fallbacks: dict[str, list[str]] = {
            locale: list(chain) for locale, chain in (fallbacks or {}).items()
        }
        self._check_fallback_cycles()
        self._chains: dict[str, tuple[str, ...]] = {}
        self._plural_rules: dict[str, object] = {}

    def _check_fallback_cycles(self) -> None:
        """Raise if the fallback configuration contains a cycle."""
        visiting: list[str] = []
        visited: set[str] = set()

        def visit(locale: str) -> None:
            if locale in visiting:
                cycle = " -> ".join([*visiting[visiting.index(locale) :], locale])
                raise MessageCatalogError(f"fallback locale cycle detected: {cycle}")
            if locale in visited:
                return
            visiting.append(locale)
            for fallback in self.fallbacks.get(locale, ()):
                visit(fallback)
            visiting.pop()
            visited.add(locale)

        for locale in self.fallbacks:
            visit(locale)

    def locale_chain(self, locale: str) -> tuple[str, ...]:
        """Return _locale_ followed by its transitive fallback locales."""
        chain = self._chains.get(locale)
        if chain is None:
            resolved: list[str] = []
            queue = [locale]
            while queue:
                loc = queue.pop(0)
                if loc in resolved:
                    continue
                resolved.append(loc)
                queue.extend(self.fallbacks.get(loc, ()))
            chain = tuple(resolved)
            self._chains[locale] = chain
        return chain

    def plural_category(self, locale: str, count: object) -> str:
        """Return the CLDR plural category for _count_ in _locale_."""
        rule = self._plural_rules.get(locale)
        if rule is None:
            try:
                rule = Locale.parse(locale.replace("-", "_")).plural_form
            except (UnknownLocaleError, ValueError) as err:
                raise MessageLookupError(f"unknown locale {locale!r}") from err
            self._plural_rules[locale] = rule
        return str(rule(count))  # type: ignore[operator]

    def required_categories(self, locale: str) -> frozenset[str]:
        """Return the plural categories _locale_ is expected to define."""
        try:
            tags = Locale.parse(locale.replace("-", "_")).plural_form.tags
        except (UnknownLocaleError, ValueError) as err:
            raise MessageCatalogError(f"unknown locale {locale!r}") from err
        return frozenset((*tags, "other"))

    def get(self, message_id: str, *, locale: str, count: object) -> str:
        """Return message text for _message_id_, _locale_ and _count_.

        Raises :class:`MessageLookupError` if the message is not defined for
        _locale_ or any of its fallback locales, or if the plural form
        selected for _count_ is missing.
        """
        forms: Optional[dict[str, str]] = None
        matched_locale = locale
        for loc in self.locale_chain(locale):
            forms = self.messages.get(message_id, {}).get(loc)
            if forms is not None:
                matched_locale = loc
                break

        if forms is None:
            raise MessageLookupError(
                f"no message {message_id!r} for locale {locale!r}"
                f" (tried {', '.join(repr(loc) for loc in self.locale_chain(locale))})"
            )

        category = self.plural_category(matched_locale, count)
        text = forms.get(category, forms.get("other"))
        if text is None:
            raise MessageLookupError(
                f"message {message_id!r} is missing plural form {category!r}"
                f" for locale {matched_locale!r}"
            )
        return text

    def placeholders(self, text: str) -> frozenset[str]:
        """Return the set of placeholder names found in message _text_."""
        return frozenset(RE_PLACEHOLDER.findall(text))

    def check(self) -> None:
        """Validate this catalog, raising :class:`MessageCatalogError`.

        Reports unknown locales, unknown or missing plural categories,
        inconsistent placeholders between plural forms and locales, and
        invalid placeholder syntax. All problems are collected and reported
        together.
        """
        problems: list[str] = []

        for message_id, locales in self.messages.items():
            expected_placeholders: Optional[frozenset[str]] = None

            for locale, forms in locales.items():
                try:
                    valid = self.required_categories(locale)
                except MessageCatalogError as err:
                    problems.append(f"message {message_id!r}: {err}")
                    continue

                unknown = forms.keys() - valid
                if unknown:
                    problems.append(
                        f"message {message_id!r} locale {locale!r}: unknown plural"
                        f" categor{'y' if len(unknown) == 1 else 'ies'}"
                        f" {', '.join(sorted(repr(c) for c in unknown))}"
                    )

                # "other" is always required. The remaining plural categories
                # are required only if the message selects on `count`.
                required = (
                    valid
                    if any("count" in self.placeholders(text) for text in forms.values())
                    else frozenset({"other"})
                )
                missing = required - forms.keys()
                if missing:
                    problems.append(
                        f"message {message_id!r} locale {locale!r}: missing plural"
                        f" categor{'y' if len(missing) == 1 else 'ies'}"
                        f" {', '.join(sorted(repr(c) for c in missing))}"
                    )

                for category, text in forms.items():
                    placeholders = self.placeholders(text)
                    if expected_placeholders is None:
                        expected_placeholders = placeholders
                    elif placeholders != expected_placeholders:
                        problems.append(
                            f"message {message_id!r} locale {locale!r} plural form"
                            f" {category!r}: placeholders"
                            f" {sorted(placeholders)!r} do not match"
                            f" {sorted(expected_placeholders)!r}"
                        )

                    for match in RE_MALFORMED_PLACEHOLDER.finditer(text):
                        if not RE_PLACEHOLDER.match(text, match.start()):
                            problems.append(
                                f"message {message_id!r} locale {locale!r} plural"
                                f" form {category!r}: malformed placeholder at"
                                f" index {match.start()}"
                            )

        if problems:
            raise MessageCatalogError(
                "message catalog check failed:\n"
                + "\n".join(f"  - {problem}" for problem in problems)
            )


def to_count(value: object) -> object:
    """Return _value_ as a number suitable for plural form selection.

    The numeric value is never rounded or truncated, so that fractional
    counts can select a different plural category than their integer part.
    """
    if isinstance(value, bool):
        raise MessageInterpolationError(
            f"message count must be a number, found {value!r}"
        )
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise MessageInterpolationError(
                f"message count must be a number, found {value!r}"
            ) from None
    raise MessageInterpolationError(
        f"message count must be a number, found {type(value).__name__}"
    )


class MessageNode(Node):
    """Parse tree node for the ``message`` tag."""

    __slots__ = ("message_id", "args", "catalog", "catalog_var", "locale_var")

    re_placeholder = RE_PLACEHOLDER
    re_interpolate = RE_INTERPOLATE

    def __init__(
        self,
        token: Token,
        *,
        message_id: Expression,
        args: list[KeywordArgument],
    ):
        super().__init__(token)
        self.message_id = message_id
        self.args = args
        self.blank = False
        # Copied from the tag at parse time.
        self.catalog: Optional[MessageCatalog] = None
        self.catalog_var = "message_catalog"
        self.locale_var = "locale"

    def __str__(self) -> str:
        args = " ".join(str(arg) for arg in self.args)
        return f"{{% message {self.message_id}{' ' + args if args else ''} %}}"

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        try:
            kwargs = {arg.name: arg.value.evaluate(context) for arg in self.args}
            message_id = self.resolve_message_id(context)
            return buffer.write(self.render_message(context, message_id, kwargs))
        except MessageError as err:
            if err.token is None:
                err.token = self.token
            raise

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer."""
        try:
            kwargs = {
                arg.name: await arg.value.evaluate_async(context) for arg in self.args
            }
            if isinstance(self.message_id, Identifier):
                message_id = self.resolve_message_id(context)
            else:
                message_id = await self.message_id.evaluate_async(context)
            return buffer.write(self.render_message(context, message_id, kwargs))
        except MessageError as err:
            if err.token is None:
                err.token = self.token
            raise

    def resolve_message_id(self, context: RenderContext) -> object:
        """Return the message ID for the current render context."""
        if isinstance(self.message_id, Identifier):
            return context.resolve(self.message_id, default=None)
        return self.message_id.evaluate(context)

    def render_message(
        self,
        context: RenderContext,
        message_id: object,
        kwargs: dict[str, object],
    ) -> str:
        """Return the localized, interpolated message text."""
        catalog = self.resolve_catalog(context)
        locale = self.resolve_locale(context, catalog, kwargs)
        count = to_count(kwargs.pop("count", 1))

        text = catalog.get(str(message_id), locale=locale, count=count)
        return self.interpolate(context, text, count, kwargs)

    def resolve_catalog(self, context: RenderContext) -> MessageCatalog:
        """Return the message catalog for the current render context."""
        catalog = context.resolve(self.catalog_var, default=self.catalog)
        if catalog is None:
            raise MessageCatalogError(
                "no message catalog: set MessageTag.catalog or provide"
                f" a {self.catalog_var!r} render context variable"
            )
        if not isinstance(catalog, MessageCatalog):
            raise MessageCatalogError(
                f"expected a MessageCatalog, found {type(catalog).__name__}"
            )
        return catalog

    def resolve_locale(
        self,
        context: RenderContext,
        catalog: MessageCatalog,
        kwargs: dict[str, object],
    ) -> str:
        """Return the locale for the current message."""
        locale = kwargs.pop("locale", None)
        if locale is None:
            locale = context.resolve(self.locale_var, default=None)
        if locale is None:
            return catalog.default_locale
        return str(locale)

    def interpolate(
        self,
        context: RenderContext,
        text: str,
        count: object,
        kwargs: dict[str, object],
    ) -> str:
        """Return message _text_ with placeholders replaced by values.

        Message text is never parsed as a Liquid template. Placeholder
        values are stringified and escaped according to the environment's
        autoescape setting.
        """
        names = self.re_placeholder.findall(text)
        values = dict(kwargs)

        if "count" in names and "count" not in values:
            values["count"] = count

        provided = set(values)
        expected = set(names)
        missing = expected - provided
        extra = provided - expected

        if missing:
            raise MessageInterpolationError(
                f"missing placeholder value{'s' if len(missing) > 1 else ''}"
                f" {', '.join(sorted(repr(name) for name in missing))}"
            )
        if extra:
            raise MessageInterpolationError(
                f"unexpected placeholder value{'s' if len(extra) > 1 else ''}"
                f" {', '.join(sorted(repr(name) for name in extra))}"
            )

        autoescape = context.env.autoescape
        escaped = {
            name: to_liquid_string(values[name], autoescape=autoescape)
            for name in names
        }

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name is None:  # "%%" escapes a literal percent sign
                return "%"
            return escaped[name]

        return self.re_interpolate.sub(replace, text)

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield self.message_id
        yield from (arg.value for arg in self.args)


class MessageTag(Tag):
    """The optional ``message`` tag.

    Looks up a message by ID in a :class:`MessageCatalog` and selects a
    plural form using Babel's plural rules for the message locale::

        {% message 'cart.items', count: cart.size %}
        {% message 'welcome', name: user.name, locale: 'ru' %}

    The message ID is the first argument, a string literal or identifier.
    Keyword arguments provide placeholder values, plus the reserved
    arguments ``count`` (used for plural form selection and available as the
    ``count`` placeholder) and ``locale``.

    The catalog is read from the :attr:`catalog` class attribute or a
    ``message_catalog`` render context variable. The locale defaults to a
    ``locale`` render context variable, falling back to the catalog's
    default locale.
    """

    name = "message"
    node_class = MessageNode
    block = False

    #: The default message catalog. Applications can set this class attribute
    #: or subclass ``MessageTag`` before registering the tag.
    catalog: Optional[MessageCatalog] = None

    #: The name of a render context variable holding a message catalog.
    catalog_var = "message_catalog"

    #: The name of a render context variable holding the current locale.
    locale_var = "locale"

    def parse(self, stream: TokenStream) -> MessageNode:
        """Parse tokens from _stream_ into an AST node."""
        token = stream.eat(TOKEN_TAG)
        tokens = stream.into_inner(tag=token, eat=False)

        message_id: Expression = parse_primitive(self.env, tokens)
        if isinstance(message_id, Path):
            head = message_id.head()
            if len(message_id.path) != 1 or not isinstance(head, str):
                raise LiquidSyntaxError(
                    "expected a message identifier, found a path",
                    token=message_id.token,
                )
            message_id = Identifier(head, token=message_id.token)
        elif not isinstance(message_id, StringLiteral):
            raise LiquidSyntaxError(
                "expected a string or identifier message ID",
                token=message_id.token,
            )

        args = KeywordArgument.parse(self.env, tokens)
        node = self.node_class(token, message_id=message_id, args=args)
        node.catalog = self.catalog
        node.catalog_var = self.catalog_var
        node.locale_var = self.locale_var
        return node
