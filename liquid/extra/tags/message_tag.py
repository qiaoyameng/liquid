"""Tag and node definition for the optional _message_ tag."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TextIO

from liquid.ast import Node
from liquid.builtin.expressions import KeywordArgument
from liquid.builtin.expressions import StringLiteral
from liquid.builtin.expressions import parse_primitive
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import TranslationError
from liquid.tag import Tag
from liquid.token import TOKEN_EOF
from liquid.token import TOKEN_TAG

from ..message_catalog import MessageCatalog
from ..message_catalog import placeholder_names
from ..message_catalog import MessageCatalogError
from ..message_catalog import check_catalog
from ..message_catalog import format_message
from ..message_catalog import resolve_message
from ..message_catalog import validate_fallback_chain

if TYPE_CHECKING:
    from liquid import Expression
    from liquid import RenderContext
    from liquid import TokenStream
    from liquid.stream import Token

TAG_MESSAGE = sys.intern("message")


class MessageNode(Node):
    """A node rendering a translated message from a message catalog."""

    __slots__ = ("message_id", "args", "tag_obj")

    def __init__(
        self,
        token: Token,
        message_id: str,
        args: dict[str, KeywordArgument],
        tag_obj: MessageTag,
    ) -> None:
        super().__init__(token)
        self.message_id = message_id
        self.args = args
        self.tag_obj = tag_obj
        self.blank = False

    def __str__(self) -> str:
        args = ", ".join([repr(self.message_id)] + [str(a) for a in self.args.values()])
        return f"{{% message {args} %}}"

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        values = {
            name: arg.value.evaluate(context) for name, arg in self.args.items()
        }
        return buffer.write(self._render(context, values))

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer asynchronously."""
        values = {
            name: await arg.value.evaluate_async(context)
            for name, arg in self.args.items()
        }
        return buffer.write(self._render(context, values))

    def _render(self, context: RenderContext, values: dict[str, object]) -> str:
        tag = self.tag_obj
        locale_tag = tag.resolve_locale(context, values)
        count = tag.resolve_count(values)
        if tag.count_var not in values:
            values[tag.count_var] = count
        catalog = tag.get_catalog(context)

        try:
            _resolved_locale, text = resolve_message(
                catalog,
                self.message_id,
                locale_tag,
                fallback_chain_map=tag.fallback_chain_map,
                default_locale=tag.default_locale,
                count=count,
            )
            format_values = dict(values)
            # `count` drives plural selection; it is only a placeholder when
            # the message text actually references it.
            if not _references(text, tag.count_var):
                format_values.pop(tag.count_var, None)
            return format_message(text, format_values, autoescape=context.autoescape)
        except MessageCatalogError as err:
            raise TranslationError(str(err), token=self.token) from err

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield from (arg.value for arg in self.args.values())


class MessageTag(Tag):
    """The optional _message_ tag.

    Args:
        catalog: The application supplied message catalog, keyed by locale tag
            and message ID. If `None`, the catalog is expected to resolve from
            a render context variable instead.
        fallback_chain_map: An explicit mapping of locale tags to the locales
            that should be tried when a translation is missing. Chains
            containing a cycle are rejected.
        default_locale: Locale tried last if no fallback produces a
            translation.
        catalog_var: Name of the render context variable holding a
            per-render catalog.
        locale_var: Name of the render context variable holding the active
            locale tag.
        count_var: Keyword argument used both as the pluralization count and
            as an ordinary placeholder value.
    """

    block = False
    name = TAG_MESSAGE
    node_class = MessageNode

    #: The default message catalog.
    catalog: Optional[MessageCatalog] = None

    #: The default fallback chain.
    fallback_chain_map: Optional[Mapping[str, list[str]]] = None

    #: Locale tried last of all.
    default_locale: Optional[str] = None

    #: Render context variable used to override the catalog per render.
    catalog_var = "message_catalog"

    #: Render context variable holding the active locale.
    locale_var = "locale"

    #: Keyword argument providing the pluralization count.
    count_var = "count"

    #: Reserved keyword argument used to override the locale for one tag.
    LOCALE_ARG = "locale"

    def __init__(
        self,
        env: object,
        *,
        catalog: Optional[MessageCatalog] = None,
        fallback_chain_map: Optional[Mapping[str, list[str]]] = None,
        default_locale: Optional[str] = None,
        catalog_var: Optional[str] = None,
        locale_var: Optional[str] = None,
        count_var: Optional[str] = None,
        validate: bool = True,
    ) -> None:
        super().__init__(env)  # type: ignore[arg-type]

        self.catalog = catalog if catalog is not None else self.catalog
        self.fallback_chain_map = (
            fallback_chain_map if fallback_chain_map is not None
            else self.fallback_chain_map
        )
        self.default_locale = (
            default_locale if default_locale is not None else self.default_locale
        )
        if catalog_var is not None:
            self.catalog_var = catalog_var
        if locale_var is not None:
            self.locale_var = locale_var
        if count_var is not None:
            self.count_var = count_var

        self._checked_catalogs: set[int] = set()
        self.validate = validate

        if self.fallback_chain_map is not None:
            validate_fallback_chain(self.fallback_chain_map)

        if validate and self.catalog is not None:
            self.check(self.catalog)

    def parse(self, stream: TokenStream) -> Node:
        """Parse a _message_ tag from _stream_."""
        token = stream.eat(TOKEN_TAG)
        tokens = stream.into_inner(tag=token, eat=False)

        if tokens.current.kind == TOKEN_EOF:
            raise LiquidSyntaxError(
                "missing message ID",
                token=token,
            )

        expression = parse_primitive(self.env, tokens)
        if not isinstance(expression, StringLiteral):
            raise LiquidSyntaxError(
                "message ID must be a quoted string literal",
                token=expression.token,
            )
        message_id = expression.value

        args = {
            arg.name: arg for arg in KeywordArgument.parse(self.env, tokens)
        }
        tokens.expect_eos()

        return self.node_class(token, message_id, args, self)

    def check(self, catalog: MessageCatalog) -> None:
        """Validate a catalog for use with this tag."""
        check_catalog(
            catalog,
            fallback_chain_map=self.fallback_chain_map,
            default_locale=self.default_locale,
        )
        self._checked_catalogs.add(id(catalog))

    def get_catalog(self, context: RenderContext) -> MessageCatalog:
        """Return the configured catalog or one resolved from the context."""
        catalog: object = context.resolve(
            self.catalog_var, token=None, default=self.catalog
        )
        if not isinstance(catalog, Mapping):
            raise TranslationError(
                "no message catalog has been configured",
                token=None,
            )

        if self.validate and id(catalog) not in self._checked_catalogs:
            check_catalog(
                catalog,  # type: ignore[arg-type]
                fallback_chain_map=self.fallback_chain_map,
                default_locale=self.default_locale,
            )
            self._checked_catalogs.add(id(catalog))

        return catalog  # type: ignore[return-value]

    def resolve_locale(
        self,
        context: RenderContext,
        values: dict[str, object],
    ) -> str:
        """Resolve the locale tag for a message render."""
        locale = values.pop(self.LOCALE_ARG, None)
        if locale is None:
            locale = context.resolve(
                self.locale_var, token=None, default=self.default_locale
            )
        if not isinstance(locale, str) or not locale:
            raise LiquidTypeError(
                "a string locale is required to render a message",
                token=None,
            )
        return str(locale)

    def resolve_count(self, values: dict[str, object]) -> object:
        """Return the raw, unrounded pluralization count."""
        return values.get(self.count_var, 0)


def _references(text: str, name: str) -> bool:
    """Return True if message _text_ contains a placeholder called _name_."""
    return name in placeholder_names(text)
