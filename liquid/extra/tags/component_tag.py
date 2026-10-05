"""Extra `component`, `slot` and `yield` tags.

These tags are not part of standard Liquid. Enable them with an `Environment`
created with `extra=True`, or register `ComponentTag`, `SlotTag` and
`YieldTag` explicitly.

A component is a partial template that decides when and where a caller's
content blocks are rendered. A caller provides one or more named slots using
the `slot` tag, and the component renders them with `yield` tags.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Optional
from typing import TextIO
from typing import Union

from liquid.ast import BlockNode
from liquid.ast import Node
from liquid.ast import Partial
from liquid.ast import PartialScope
from liquid.builtin.expressions import Identifier
from liquid.builtin.expressions import KeywordArgument
from liquid.builtin.expressions import Path
from liquid.builtin.expressions import StringLiteral
from liquid.builtin.expressions import parse_name
from liquid.builtin.expressions import parse_primitive
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import TemplateNotFoundError
from liquid.parser import get_parser
from liquid.tag import Tag
from liquid.template import BoundTemplate
from liquid.token import TOKEN_COLON
from liquid.token import TOKEN_EOF
from liquid.token import TOKEN_EXPRESSION
from liquid.token import TOKEN_TAG
from liquid.utils import ReadOnlyChainMap

if TYPE_CHECKING:
    from liquid.context import RenderContext
    from liquid.environment import Environment
    from liquid.expression import Expression
    from liquid.stream import TokenStream
    from liquid.token import Token


TAG_COMPONENT = sys.intern("component")
TAG_ENDCOMPONENT = sys.intern("endcomponent")
TAG_SLOT = sys.intern("slot")
TAG_ENDSLOT = sys.intern("endslot")
TAG_YIELD = sys.intern("yield")
TAG_ENDYIELD = sys.intern("endyield")

# `None` is used as the name of the default, unnamed slot.
DEFAULT_SLOT: Optional[str] = None

COMPONENTS_NAMESPACE = "components"


@dataclass
class Slot:
    """A caller-provided block of content captured at definition time."""

    block: BlockNode
    context: RenderContext


class ComponentNode(Node):
    """The extra _component_ tag."""

    __slots__ = ("name", "args", "slots", "default_slot")
    tag = TAG_COMPONENT

    def __init__(
        self,
        token: Token,
        name: Union[StringLiteral, Identifier],
        args: list[KeywordArgument],
        slots: dict[str, Slot],
        default_slot: Optional[Slot],
    ):
        super().__init__(token)
        self.name = name
        self.args = args
        self.slots = slots
        self.default_slot = default_slot
        self.blank = False

    def __str__(self) -> str:
        args = ", ".join(str(arg) for arg in self.args)
        parts = []
        for name, slot in self.slots.items():
            parts.append(
                f"{{% slot {name} %}}{slot.block}{{% endslot %}}"
            )
        if self.default_slot is not None:
            parts.append(
                f"{{% slot %}}{self.default_slot.block}{{% endslot %}}"
            )
        slots = "".join(parts)
        sep = ", " if args else ""
        return (
            f"{{% component {self.name}{sep}{args} %}}"
            f"{slots}{{% endcomponent %}}"
        )

    def _load_template(self, context: RenderContext) -> BoundTemplate:
        if isinstance(self.name, Identifier):
            template: Optional[BoundTemplate] = context.resolve(
                self.name, token=self.token, default=None
            )
            if not isinstance(template, BoundTemplate):
                raise TemplateNotFoundError(
                    self.name,
                    filename=context.template.full_name(),
                    token=self.name.token,
                )
            return template

        try:
            return context.env.get_template(
                self.name.value, context=context, tag=self.tag
            )
        except TemplateNotFoundError as err:
            err.token = self.name.token
            err.template_name = context.template.full_name()
            raise

    def _capture_slots(self, context: RenderContext) -> dict[Optional[str], Slot]:
        # Slot bodies keep the scope of the caller at definition time. A shallow
        # copy of the slots map lets a component render other components without
        # this call's slots leaking to unrelated components.
        slots: dict[Optional[str], Slot] = {
            name: Slot(slot.block, context)
            for name, slot in self.slots.items()
        }
        if self.default_slot is not None:
            slots[DEFAULT_SLOT] = Slot(self.default_slot.block, context)
        return slots

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        template = self._load_template(context)

        # Keyword arguments are evaluated in the caller's scope, once.
        args = {arg.name: arg.value.evaluate(context) for arg in self.args}

        # Isolated namespace, just like the built-in `render` tag. The component
        # sees only its named arguments and template globals; assigns inside the
        # component can not leak back to the caller.
        component_context = context.copy(
            ReadOnlyChainMap(args),
            carry_loop_iterations=True,
            template=template,
        )

        # Isolated copies do not share the tag namespace, so propagate the slot
        # stack explicitly before pushing this call's slots.
        stack = context.tag_namespace[COMPONENTS_NAMESPACE]
        component_context.tag_namespace[COMPONENTS_NAMESPACE] = stack
        stack.append(self._capture_slots(context))
        try:
            template.render_with_context(
                component_context, buffer, partial=True, block_scope=True
            )
        finally:
            stack.pop()

        return True

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer."""
        template = self._load_template(context)

        args = {
            arg.name: await arg.value.evaluate_async(context) for arg in self.args
        }

        component_context = context.copy(
            ReadOnlyChainMap(args),
            carry_loop_iterations=True,
            template=template,
        )

        stack = context.tag_namespace[COMPONENTS_NAMESPACE]
        component_context.tag_namespace[COMPONENTS_NAMESPACE] = stack
        stack.append(self._capture_slots(context))
        try:
            await template.render_with_context_async(
                component_context, buffer, partial=True, block_scope=True
            )
        finally:
            stack.pop()

        return True

    def children(
        self, static_context: RenderContext, *, include_partials: bool = True
    ) -> Iterable[Node]:
        """Return this node's children."""
        for slot in self.slots.values():
            yield slot.block
        if self.default_slot is not None:
            yield self.default_slot.block

        if isinstance(self.name, Identifier):
            template: Optional[BoundTemplate] = static_context.resolve(
                self.name, token=self.token, default=None
            )
            if template:
                yield from template.nodes
        elif include_partials:
            name = self.name.evaluate(static_context)
            try:
                template = static_context.env.get_template(
                    str(name), context=static_context, tag=self.tag
                )
                yield from template.nodes
            except TemplateNotFoundError as err:
                err.token = self.name.token
                err.template_name = static_context.template.full_name()
                raise

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield from (arg.value for arg in self.args)

    def partial_scope(self) -> Optional[Partial]:
        """Return information about the component's partial template."""
        scope = [Identifier(arg.name, token=arg.token) for arg in self.args]
        partial_name = self.name.value if isinstance(self.name, StringLiteral) else ""
        partial_key = hash(
            (partial_name, *[arg.name for arg in self.args])
        )
        return Partial(
            name=partial_name,
            scope=PartialScope.ISOLATED,
            in_scope=scope,
            key=partial_key,
        )


class ComponentTag(Tag):
    """The extra _component_ tag."""

    name = TAG_COMPONENT
    end = TAG_ENDCOMPONENT
    node_class = ComponentNode

    ENDBLOCK = frozenset((TAG_ENDCOMPONENT, TOKEN_EOF))

    def parse(self, stream: TokenStream) -> Node:
        """Parse tokens from _stream_ into an AST node."""
        token = stream.eat(TOKEN_TAG)
        tokens = stream.into_inner(tag=token, eat=False)
        name: Union[Path, StringLiteral] = parse_primitive(self.env, tokens)

        if isinstance(name, Path):
            head = name.head()
            if len(name.path) != 1 or not isinstance(head, str):
                raise LiquidSyntaxError(
                    "expected an identifier, found a path",
                    token=name.token,
                )
            name = Identifier(head, token=name.token)
        elif not isinstance(name, StringLiteral):
            raise LiquidSyntaxError(
                "expected a string or identifier", token=name.token
            )

        args = KeywordArgument.parse(self.env, tokens)
        next(stream)  # Consume the expression token.
        block = get_parser(self.env).parse_block(stream, self.ENDBLOCK)
        stream.expect(TOKEN_TAG, value=self.end)

        slots: dict[str, Slot] = {}
        default_slot: Optional[Slot] = None

        for node in block.nodes:
            if isinstance(node, SlotNode):
                if node.slot_name in slots:
                    raise LiquidSyntaxError(
                        f"duplicate slot {node.slot_name!r}",
                        token=node.token,
                    )
                if node.slot_name == DEFAULT_SLOT:
                    if default_slot is not None:
                        raise LiquidSyntaxError(
                            "duplicate default slot", token=node.token
                        )
                    default_slot = Slot(node.block, None)
                else:
                    slots[node.slot_name] = Slot(node.block, None)
            elif not node.blank:
                # Only top-level `slot` blocks and whitespace are allowed in a
                # component's body.
                raise LiquidSyntaxError(
                    "expected a 'slot' tag inside 'component'", token=node.token
                )

        return self.node_class(token, name, args, slots, default_slot)


class SlotNode(Node):
    """The extra _slot_ tag.

    A `slot` only has special meaning as an immediate child of a `component`
    block. Elsewhere it renders nothing.
    """

    __slots__ = ("slot_name", "block")
    tag = TAG_SLOT

    def __init__(self, token: Token, slot_name: Optional[str], block: BlockNode):
        super().__init__(token)
        self.slot_name = slot_name
        self.block = block
        self.blank = block.blank

    def __str__(self) -> str:
        name = f" {self.slot_name}" if self.slot_name is not None else ""
        return f"{{% slot{name} %}}{self.block}{{% endslot %}}"

    def render_to_output(self, _context: RenderContext, _buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        # Slots are rendered on demand by a component's `yield` tag.
        return 0

    def children(
        self, static_context: RenderContext, *, include_partials: bool = True
    ) -> Iterable[Node]:
        """Return this node's children."""
        yield self.block


class SlotTag(Tag):
    """The extra _slot_ tag."""

    name = TAG_SLOT
    end = TAG_ENDSLOT
    node_class = SlotNode

    ENDBLOCK = frozenset((TAG_ENDSLOT, TOKEN_EOF))

    def parse(self, stream: TokenStream) -> Node:
        """Parse tokens from _stream_ into an AST node."""
        token = stream.eat(TOKEN_TAG)
        slot_name: Optional[str] = None

        # An empty `{% slot %}` produces no expression token at all.
        if stream.current.kind == TOKEN_EXPRESSION:
            tokens = stream.into_inner(tag=token, eat=False)
            slot_name = parse_name(self.env, tokens)
            tokens.expect_eos()
            next(stream)  # Consume the expression token.

        block = get_parser(self.env).parse_block(stream, self.ENDBLOCK)
        stream.expect(TOKEN_TAG, value=self.end)
        return self.node_class(token, slot_name, block)




class YieldNode(Node):
    """The extra _yield_ tag."""

    __slots__ = ("slot_name", "args", "default")
    tag = TAG_YIELD

    def __init__(
        self,
        token: Token,
        slot_name: Optional[str],
        args: list[KeywordArgument],
        default: Optional[BlockNode],
    ):
        super().__init__(token)
        self.slot_name = slot_name
        self.args = args
        self.default = default
        self.blank = False

    def __str__(self) -> str:
        name = f" {self.slot_name}" if self.slot_name is not None else ""
        args = (
            ", " + ", ".join(str(arg) for arg in self.args) if self.args else ""
        )
        if self.default is not None:
            return (
                f"{{% yield{name}{args} %}}{self.default}{{% endyield %}}"
            )
        return f"{{% yield{name}{args} %}}"

    def _find_slot(self, context: RenderContext) -> Optional[Slot]:
        # A yield resolves against the innermost component being rendered only.
        # Frames belonging to outer components are not searched.
        stack = context.tag_namespace.get(COMPONENTS_NAMESPACE)
        if stack and self.slot_name in stack[-1]:
            return stack[-1][self.slot_name]
        return None

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        slot = self._find_slot(context)

        if slot is None:
            # Unknown yield names and slots the caller did not provide render
            # the component-defined default, or nothing.
            if self.default is not None:
                return self.default.render(context, buffer)
            return 0

        # Limited named parameters are visible only inside the yielded slot.
        params = {arg.name: arg.value.evaluate(context) for arg in self.args}

        # A block-scoped copy retains the caller scope captured at the slot's
        # definition while yield parameters shadow it. Fresh locals per yield
        # mean assigns inside a slot can not affect the next yield.
        slot_context = slot.context.copy(
            ReadOnlyChainMap(params),
            carry_loop_iterations=True,
            block_scope=True,
        )
        return slot.block.render(slot_context, buffer)

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer."""
        slot = self._find_slot(context)

        if slot is None:
            if self.default is not None:
                return await self.default.render_async(context, buffer)
            return 0

        params = {
            arg.name: await arg.value.evaluate_async(context)
            for arg in self.args
        }

        slot_context = slot.context.copy(
            ReadOnlyChainMap(params),
            carry_loop_iterations=True,
            block_scope=True,
        )
        return await slot.block.render_async(slot_context, buffer)

    def children(
        self, static_context: RenderContext, *, include_partials: bool = True
    ) -> Iterable[Node]:
        """Return this node's children."""
        if self.default is not None:
            yield self.default

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield from (arg.value for arg in self.args)


def _make_block_ends(env: object) -> tuple[dict[str, str], set[str]]:
    """Map registered block tags to their end tags."""
    open_to_end: dict[str, str] = {}
    for tag in getattr(env, "tags", {}).values():
        end_tag = getattr(tag, "end", "")
        if getattr(tag, "block", False) and end_tag and tag.name != TAG_YIELD:
            open_to_end[tag.name] = end_tag
    return open_to_end, set(open_to_end.values())


def _find_endyield(
    toks: list[Token],
    start: int,
    open_to_end: dict[str, str],
    all_end_tags: set[str],
) -> Optional[int]:
    """Find the `endyield` that closes the block-form yield at or after _start_.

    The token at _start_ must be the first token of the yield's body (its
    expression token, or the content immediately following an empty
    expression). Returns `None` for the inline form.

    Each open `yield` tracks its own stack of nested block tags and whether a
    nested block yield has been seen. An `endyield` closes the innermost open
    yield; a yield left unclosed at end of input is inline.
    """
    # One entry per open yield: (list of pending end tags, found_block_child).
    yields: list[list[str]] = [[]]

    pos = start
    while pos < len(toks):
        tok = toks[pos]
        if tok.kind != TOKEN_TAG:
            pos += 1
            continue

        name = tok.value

        if name == TAG_YIELD:
            yields.append([])
        elif name in open_to_end:
            yields[-1].append(open_to_end[name])
        elif name in all_end_tags or name == TAG_ENDYIELD:
            stack = yields[-1]
            if stack and stack[-1] == name:
                stack.pop()
            elif name == TAG_ENDYIELD and not stack:
                if len(yields) == 1:
                    return pos
                yields.pop()
            else:
                return None

        pos += 1

    return None


class YieldTag(Tag):
    """The extra _yield_ tag."""

    name = TAG_YIELD
    end = TAG_ENDYIELD
    node_class = YieldNode
    block = False

    ENDBLOCK = frozenset((TAG_ENDYIELD, TOKEN_EOF))

    def __init__(self, env: Environment):
        super().__init__(env)
        self.open_to_end, self.end_tags = _make_block_ends(env)

    def parse(self, stream: TokenStream) -> Node:
        """Parse tokens from _stream_ into an AST node.

        On return the stream is positioned on the last token the tag owns:
        the expression token for an inline yield, the yield tag for an inline
        yield without an expression, or the closing `endyield` tag for the
        block form. The parser loop advances once from there.
        """
        tag_pos = stream.pos
        token = stream.eat(TOKEN_TAG)
        slot_name: Optional[str] = None
        args: list[KeywordArgument] = []

        if stream.current.kind == TOKEN_EXPRESSION:
            expr_pos = stream.pos
            tokens = stream.into_inner(tag=token, eat=False)
            if tokens.current.kind != TOKEN_EOF:
                # A leading quoted string or word is the optional slot name,
                # provided it does not look like a keyword argument.
                if tokens.peek.kind != TOKEN_COLON:
                    slot_name = parse_name(self.env, tokens)
            args = KeywordArgument.parse(self.env, tokens)
        else:
            expr_pos = None

        body_start = expr_pos + 1 if expr_pos is not None else stream.pos
        end_pos = _find_endyield(
            stream.tokens, body_start, self.open_to_end, self.end_tags
        )

        default: Optional[BlockNode] = None
        if end_pos is not None:
            if expr_pos is not None:
                next(stream)  # Consume the expression token for block form.
            block = get_parser(self.env).parse_block(stream, self.ENDBLOCK)
            stream.expect(TOKEN_TAG, value=TAG_ENDYIELD)
            default = block
        elif expr_pos is None:
            # Inline yield without an expression. `eat` consumed the tag, so
            # rewind to it; the parser loop advances to the following token.
            stream.pos = tag_pos

        return self.node_class(token, slot_name, args, default)
