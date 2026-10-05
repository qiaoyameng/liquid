"""The opt-in `component`, `slot` and `yield` tags."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Optional
from typing import TextIO

from liquid.ast import BlockNode
from liquid.ast import Node
from liquid.ast import Partial
from liquid.ast import PartialScope
from liquid.builtin.expressions import KeywordArgument
from liquid.builtin.expressions import parse_string_or_path
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import TemplateNotFoundError
from liquid.parser import get_parser
from liquid.tag import Tag
from liquid.token import TOKEN_EOF
from liquid.token import TOKEN_EXPRESSION
from liquid.token import TOKEN_STRING
from liquid.token import TOKEN_TAG
from liquid.token import TOKEN_WORD
from liquid.token import Token
from liquid.utils import ReadOnlyChainMap

if TYPE_CHECKING:
    from liquid.context import RenderContext
    from liquid.expression import Expression
    from liquid.stream import TokenStream
    from liquid.template import BoundTemplate

TAG_COMPONENT = sys.intern("component")
TAG_ENDCOMPONENT = sys.intern("endcomponent")
TAG_SLOT = sys.intern("slot")
TAG_ENDSLOT = sys.intern("endslot")
TAG_YIELD = sys.intern("yield")
TAG_ENDYIELD = sys.intern("endyield")

COMPONENT_BLOCK = frozenset((TAG_ENDCOMPONENT, TOKEN_EOF))
SLOT_BLOCK = frozenset((TAG_ENDSLOT, TOKEN_EOF))

# Key used for the unnamed/default slot and for non-slot content found directly
# in a `component` tag's body.
DEFAULT_SLOT = ""


class SlotContent:
    """A slot block captured at the call site of a component."""

    __slots__ = ("block", "context")

    def __init__(self, block: BlockNode, context: RenderContext):
        self.block = block
        # The render context where the slot was defined. Slot blocks keep
        # this caller's lexical scope whenever the component renders them.
        self.context = context


class ComponentNode(Node):
    """The opt-in _component_ tag."""

    __slots__ = ("name", "args", "slots", "default_block")
    tag = TAG_COMPONENT

    def __init__(
        self,
        token: Token,
        name: Expression,
        args: list[KeywordArgument],
        slots: dict[str, BlockNode],
        default_block: BlockNode,
    ):
        super().__init__(token)
        self.name = name
        self.args = args
        self.slots = slots
        self.default_block = default_block
        self.blank = False

    def __str__(self) -> str:
        args = ", " + ", ".join(str(arg) for arg in self.args) if self.args else ""
        slots = "".join(
            f"{{% slot {name} %}}{block}{{% endslot %}}"
            for name, block in self.slots.items()
        )
        return (
            f"{{% component {self.name}{args} %}}"
            f"{self.default_block}{slots}"
            f"{{% endcomponent %}}"
        )

    def _slots(self, context: RenderContext) -> dict[str, SlotContent]:
        slots = {
            name: SlotContent(block, context)
            for name, block in self.slots.items()
        }
        if self.default_block.nodes:
            slots[DEFAULT_SLOT] = SlotContent(self.default_block, context)
        return slots

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        try:
            template = context.env.get_template(
                str(self.name.evaluate(context)),
                context=context,
                tag=self.tag,
            )
        except TemplateNotFoundError as err:
            err.token = self.name.token
            err.template_name = context.template.full_name()
            raise

        args = {arg.name: arg.value.evaluate(context) for arg in self.args}
        _render_loaded_component(self, context, buffer, template, args)
        return True

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer."""
        try:
            template = await context.env.get_template_async(
                str(await self.name.evaluate_async(context)),
                context=context,
                tag=self.tag,
            )
        except TemplateNotFoundError as err:
            err.token = self.name.token
            err.template_name = context.template.full_name()
            raise

        args = {
            arg.name: await arg.value.evaluate_async(context) for arg in self.args
        }
        _render_loaded_component(self, context, buffer, template, args)
        return True

    def children(
        self, static_context: RenderContext, *, include_partials: bool = True
    ) -> Iterable[Node]:
        """Return this node's children."""
        yield self.default_block
        for block in self.slots.values():
            yield block
        if include_partials:
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

    async def children_async(
        self, static_context: RenderContext, *, include_partials: bool = True
    ) -> Iterable[Node]:
        """Return this node's children."""
        children: list[Node] = [self.default_block]
        children.extend(self.slots.values())
        if include_partials:
            name = await self.name.evaluate_async(static_context)
            try:
                template = await static_context.env.get_template_async(
                    str(name), context=static_context, tag=self.tag
                )
                children.extend(template.nodes)
            except TemplateNotFoundError as err:
                err.token = self.name.token
                err.template_name = static_context.template.full_name()
                raise
        return children

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield self.name
        yield from (arg.value for arg in self.args)

    def partial_scope(self) -> Partial | None:
        """Return information about a partial template loaded by this node."""
        return Partial(name=self.name, scope=PartialScope.ISOLATED, in_scope=[])


def _render_loaded_component(
    node: ComponentNode,
    context: RenderContext,
    buffer: TextIO,
    template: BoundTemplate,
    args: dict[str, object],
) -> None:
    """Push the component's isolated scope and render its template."""
    # `copy` enforces the existing render/context depth limit, so recursive
    # components fail in the same way recursive `render` tags do. Keyword
    # arguments are the only variables in the component's initial scope.
    component_context = context.copy(
        ReadOnlyChainMap(args),
        carry_loop_iterations=True,
        template=template,
    )

    # Slot blocks are rendered in the caller's scope, not the component's. Store
    # the captured blocks in the component context where `yield` tags can find
    # them.
    component_context.tag_namespace["components"] = context.tag_namespace.get(
        "components", ()
    ) + (node._slots(context),)

    template.render_with_context(
        component_context, buffer, partial=True, block_scope=True
    )


class SlotNode(Node):
    """The opt-in _slot_ tag.

    `slot` tags are only meaningful inside the body of a `component` tag, where
    they capture a block for the component to render. Elsewhere they render
    nothing.
    """

    __slots__ = ("name", "block")
    tag = TAG_SLOT

    def __init__(self, token: Token, name: str, block: BlockNode):
        super().__init__(token)
        self.name = name
        self.block = block
        self.blank = True

    def __str__(self) -> str:
        return f"{{% slot {self.name} %}}{self.block}{{% endslot %}}"

    def render_to_output(
        self, context: RenderContext, buffer: TextIO
    ) -> int:  # pragma: no cover
        """Slot blocks are rendered by `yield`, never directly."""
        return 0

    def children(
        self,
        static_context: RenderContext,  # noqa: ARG002
        *,
        include_partials: bool = True,  # noqa: ARG002
    ) -> Iterable[Node]:
        """Return this node's children."""
        yield self.block


class YieldNode(Node):
    """The opt-in _yield_ tag."""

    __slots__ = ("name", "args", "default_block")
    tag = TAG_YIELD

    def __init__(
        self,
        token: Token,
        name: str,
        args: list[KeywordArgument],
        default_block: Optional[BlockNode],
    ):
        super().__init__(token)
        self.name = name
        self.args = args
        self.default_block = default_block
        self.blank = False

    def __str__(self) -> str:
        args = ", " + ", ".join(str(arg) for arg in self.args) if self.args else ""
        if self.default_block is None:
            return f"{{% yield {self.name}{args} %}}"
        return (
            f"{{% yield {self.name}{args} %}}"
            f"{self.default_block}"
            f"{{% endyield %}}"
        )

    def render_to_output(self, context: RenderContext, buffer: TextIO) -> int:
        """Render the node to the output buffer."""
        slot_map = context.tag_namespace.get("components", ())[-1:]
        if slot_map:
            slot = slot_map[0].get(self.name)
            if slot is not None:
                _render_slot(
                    slot.block,
                    slot.context,
                    buffer,
                    {arg.name: arg.value.evaluate(context) for arg in self.args},
                )
                return True

        if self.default_block is not None:
            return self.default_block.render(context, buffer)
        return 0

    async def render_to_output_async(
        self, context: RenderContext, buffer: TextIO
    ) -> int:
        """Render the node to the output buffer."""
        slot_map = context.tag_namespace.get("components", ())[-1:]
        if slot_map:
            slot = slot_map[0].get(self.name)
            if slot is not None:
                await _render_slot_async(
                    slot.block,
                    slot.context,
                    buffer,
                    {
                        arg.name: await arg.value.evaluate_async(context)
                        for arg in self.args
                    },
                )
                return True

        if self.default_block is not None:
            return await self.default_block.render_async(context, buffer)
        return 0

    def children(
        self,
        static_context: RenderContext,  # noqa: ARG002
        *,
        include_partials: bool = True,  # noqa: ARG002
    ) -> Iterable[Node]:
        """Return this node's children."""
        if self.default_block is not None:
            yield self.default_block

    def expressions(self) -> Iterable[Expression]:
        """Return this node's expressions."""
        yield from (arg.value for arg in self.args)


def _render_slot(
    block: BlockNode,
    caller_context: RenderContext,
    buffer: TextIO,
    params: dict[str, object],
) -> None:
    """Render a captured slot block in the caller's scope.

    The block keeps the lexical scope of the call site, with the yield's
    keyword arguments layered on top. Assignments inside the block go into a
    fresh local namespace that is discarded after rendering, so a slot yielded
    more than once always starts with the same scope.
    """
    slot_context = caller_context.copy(
        ReadOnlyChainMap(params, caller_context.locals),
        carry_loop_iterations=True,
    )

    # Share macro definitions and template inheritance blocks with the caller,
    # but keep stateful tags and the component stack isolated per yield.
    slot_context.tag_namespace["macros"] = caller_context.tag_namespace["macros"]
    slot_context.tag_namespace["extends"] = caller_context.tag_namespace["extends"]

    block.render(slot_context, buffer)


async def _render_slot_async(
    block: BlockNode,
    caller_context: RenderContext,
    buffer: TextIO,
    params: dict[str, object],
) -> None:
    """An async version of `_render_slot`."""
    slot_context = caller_context.copy(
        ReadOnlyChainMap(params, caller_context.locals),
        carry_loop_iterations=True,
    )
    slot_context.tag_namespace["macros"] = caller_context.tag_namespace["macros"]
    slot_context.tag_namespace["extends"] = caller_context.tag_namespace["extends"]
    await block.render_async(slot_context, buffer)


class ComponentTag(Tag):
    """The opt-in _component_ tag."""

    name = TAG_COMPONENT
    end = TAG_ENDCOMPONENT
    block = True
    node_class = ComponentNode

    def parse(self, stream: TokenStream) -> ComponentNode:
        """Parse tokens from _stream_ into a ComponentNode."""
        token = stream.eat(TOKEN_TAG)
        tokens = stream.into_inner(tag=token)
        name = parse_string_or_path(self.env, tokens)
        args = KeywordArgument.parse(self.env, tokens)

        block = get_parser(self.env).parse_block(stream, COMPONENT_BLOCK)

        # Extract named slots from the component body. Any nodes not inside a
        # `slot` tag become the unnamed/default slot.
        slots: dict[str, BlockNode] = {}
        default_nodes: list[Node] = []
        for node in block.nodes:
            if isinstance(node, SlotNode):
                slots[node.name] = node.block
            else:
                default_nodes.append(node)

        stream.expect(TOKEN_TAG, value=TAG_ENDCOMPONENT)
        return self.node_class(
            token,
            name,
            args,
            slots,
            BlockNode(token, default_nodes),
        )


class SlotTag(Tag):
    """The opt-in _slot_ tag."""

    name = TAG_SLOT
    end = TAG_ENDSLOT
    block = True
    node_class = SlotNode

    def parse(self, stream: TokenStream) -> SlotNode:
        """Parse tokens from _stream_ into a SlotNode."""
        token = stream.eat(TOKEN_TAG)
        tokens = stream.into_inner(tag=token)
        name = _parse_slot_name(tokens)
        tokens.expect_eos()
        block = get_parser(self.env).parse_block(stream, SLOT_BLOCK)
        stream.expect(TOKEN_TAG, value=TAG_ENDSLOT)
        return self.node_class(token, name, block)


class YieldTag(Tag):
    """The opt-in _yield_ tag."""

    name = TAG_YIELD
    end = TAG_ENDYIELD
    block = True
    node_class = YieldNode

    def parse(self, stream: TokenStream) -> YieldNode:
        """Parse tokens from _stream_ into a YieldNode."""
        token = stream.eat(TOKEN_TAG)
        name = DEFAULT_SLOT
        args: list[KeywordArgument] = []
        has_expression = stream.current.kind == TOKEN_EXPRESSION

        if has_expression:
            # Don't consume the expression token yet. An inline yield must leave
            # the expression as the stream's current token, just like
            # `include`, so the parser's main loop advances over it rather than
            # over the content following the yield.
            tokens = stream.into_inner(tag=token, eat=False)
            if tokens.current.kind != TOKEN_EOF:
                name = _parse_slot_name(tokens)
                args = KeywordArgument.parse(self.env, tokens)

        if _has_end_tag(self.env, stream, has_expression):
            if has_expression:
                # Block-form yield: consume the expression ourselves, parse the
                # default content and leave `endyield` as the current token for
                # the parser's main loop to advance over, like other block
                # tags do.
                next(stream)
            block = get_parser(self.env).parse_block(
                stream, frozenset((TAG_ENDYIELD, TOKEN_EOF))
            )
            stream.expect(TOKEN_TAG, value=TAG_ENDYIELD)
        else:
            block = None
            if not has_expression:
                # We consumed the start tag but there is no expression token for
                # the parser's main loop to advance over. Rewind so that the
                # main loop moves on to the content following the yield.
                stream.pos -= 1

        return self.node_class(token, name, args, block)


def _parse_slot_name(tokens: TokenStream) -> str:
    """Parse a bare word or quoted string literal used as a slot name."""
    token = next(tokens)
    if token.kind in (TOKEN_WORD, TOKEN_STRING):
        return token.value
    raise LiquidSyntaxError(
        f"expected a slot name, found {token.kind}", token=token
    )


def _open_block_ends(env, stream: TokenStream, until: int) -> frozenset[str]:
    """Return end tag names of blocks open before token index _until_.

    The parser does not tell a tag which blocks enclose it, so reconstruct the
    block stack by scanning the template's tags from the beginning.
    """
    end_of: dict[str, str] = {}
    for tag in env.tags.values():
        end_name = getattr(tag, "end", "")
        if getattr(tag, "block", False) and end_name and end_name != tag.name:
            end_of.setdefault(end_name, tag.name)

    open_ends: list[str] = []
    tokens = stream.tokens
    for idx in range(until):
        token = tokens[idx]
        if token.kind != TOKEN_TAG:
            continue
        tag_obj = env.tags.get(token.value)
        if tag_obj is not None and getattr(tag_obj, "block", False):
            end_name = getattr(tag_obj, "end", "")
            if end_name and end_name != token.value:
                open_ends.append(end_name)
        elif token.value in end_of:
            for depth in range(len(open_ends) - 1, -1, -1):
                if open_ends[depth] == token.value:
                    del open_ends[depth:]
                    break

    return frozenset(open_ends)


def _has_end_tag(env, stream: TokenStream, has_expression: bool) -> bool:
    """Return True if this yield tag has default content.

    The yield is block-form if an `endyield` appears at this yield's nesting
    level before the end of the innermost enclosing block. The default content
    itself may contain any other tags, including nested block-form yields.
    """
    # The yield's start tag is immediately before the current position. The
    # block stack at that point does not include this yield.
    enclosing_ends = _open_block_ends(env, stream, stream.pos - 1)

    # Skip the expression token (if any) to reach the yield's body.
    position = stream.pos + 1 if has_expression else stream.pos
    depth = 0
    tokens = stream.tokens
    while position < len(tokens):
        token = tokens[position]
        if token.kind == TOKEN_EOF:
            return False
        if token.kind == TOKEN_TAG:
            if token.value == TAG_ENDYIELD:
                if depth == 0:
                    return True
                depth -= 1
            elif token.value == TAG_YIELD:
                depth += 1
            elif token.value in enclosing_ends:
                return False
        position += 1
    return False
