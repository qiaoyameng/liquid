"""Static analysis of HTML context for contextual autoescape mode.

When `Environment.contextual_autoescape` is enabled, every template is analyzed
at parse time. Literal output is fed to an `HTMLContextTracker` and every output
statement is annotated with the `HTMLContextKind` it appears in. Branches,
loops, captures and partials are checked for HTML context consistency, and
output in unsupported positions is rejected with `HTMLContextError`.
"""

from __future__ import annotations

from typing import Iterable
from typing import List
from typing import Optional

from .ast import BlockNode
from .ast import Node
from .builtin.content import ContentNode
from .builtin.output import OutputNode
from .builtin.tags.assign_tag import AssignNode
from .builtin.tags.capture_tag import CaptureNode
from .builtin.tags.case_tag import CaseNode
from .builtin.tags.case_tag import MultiExpressionBlockNode
from .builtin.tags.comment_tag import CommentNode
from .builtin.tags.cycle_tag import CycleNode
from .builtin.tags.decrement_tag import DecrementNode
from .builtin.tags.doc_tag import DocNode
from .builtin.tags.for_tag import BreakNode
from .builtin.tags.for_tag import ContinueNode
from .builtin.tags.for_tag import ForNode
from .builtin.tags.if_tag import IfNode
from .builtin.tags.ifchanged_tag import IfChangedNode
from .builtin.tags.include_tag import IncludeNode
from .builtin.tags.increment_tag import IncrementNode
from .builtin.tags.liquid_tag import LiquidNode
from .builtin.tags.render_tag import RenderNode
from .builtin.tags.tablerow_tag import TablerowNode
from .builtin.tags.unless_tag import UnlessNode
from .exceptions import HTMLContextError
from .html_context import HTMLContextKind
from .html_context import HTMLContextTracker

# Nodes that never produce output, so have no effect on HTML context.
_NO_OUTPUT = (
    AssignNode,
    BreakNode,
    CommentNode,  # includes InlineCommentNode
    ContinueNode,
    DocNode,
)


def analyze_html_context(nodes: List[Node]) -> None:
    """Analyze _nodes_, annotating output nodes with their HTML context.

    Raise `HTMLContextError` if any output appears in an unsupported HTML
    position, if HTML structure is inconsistent across branches, or if the
    template does not start and end in HTML text context.
    """
    tracker = HTMLContextTracker()
    _analyze_nodes(nodes, tracker)
    if not tracker.at_text():
        token = nodes[-1].token if nodes else None
        raise HTMLContextError(
            f"template does not end in HTML text context "
            f"(left the parser in {tracker.describe()})",
            token=token,
        )


def _analyze_nodes(nodes: Iterable[Node], tracker: HTMLContextTracker) -> None:
    for node in nodes:
        _analyze_node(node, tracker)


def _analyze_block(block: BlockNode, tracker: HTMLContextTracker) -> None:
    _analyze_nodes(block.nodes, tracker)


def _output_kind(node: Node, tracker: HTMLContextTracker) -> HTMLContextKind:
    kind = tracker.output_kind()
    if kind is None:
        raise HTMLContextError(
            f"output in unsupported HTML position: {tracker.describe()}",
            token=node.token,
        )
    return kind


def _analyze_branches(
    node: Node,
    tracker: HTMLContextTracker,
    blocks: Iterable[Optional[BlockNode]],
    *,
    implicit_empty_branch: bool,
) -> None:
    """Analyze conditional branches, requiring a consistent end context."""
    start = tracker.snapshot()
    outcomes = []
    for block in blocks:
        if block is None:
            continue
        tracker.restore(start)
        _analyze_block(block, tracker)
        outcomes.append(tracker.snapshot())
    if implicit_empty_branch or not outcomes:
        # The path where no branch is taken leaves the context unchanged.
        outcomes.append(start)
    merged = outcomes[0]
    if any(outcome != merged for outcome in outcomes[1:]):
        raise HTMLContextError(
            "inconsistent HTML context at the end of conditional branches",
            token=node.token,
        )
    tracker.restore(merged)


def _analyze_balanced(
    node: Node, tracker: HTMLContextTracker, block: BlockNode
) -> None:
    """Analyze a block that must leave the HTML context unchanged."""
    start = tracker.snapshot()
    _analyze_block(block, tracker)
    if tracker.snapshot() != start:
        raise HTMLContextError(
            f"unbalanced HTML in {node.token.value!r} tag: the block must end "
            f"in the same HTML context it starts in",
            token=node.token,
        )
    tracker.restore(start)


def _analyze_node(node: Node, tracker: HTMLContextTracker) -> None:
    if isinstance(node, ContentNode):
        tracker.feed(node.text)
    elif isinstance(node, OutputNode):  # includes EchoNode
        node.html_context = _output_kind(node, tracker)
    elif isinstance(node, CycleNode):
        node.html_context = _output_kind(node, tracker)
    elif isinstance(node, (IncrementNode, DecrementNode)):
        # Counters only ever output integers, but their position must still be
        # a supported one.
        _output_kind(node, tracker)
    elif isinstance(node, (IfNode, UnlessNode)):
        _analyze_branches(
            node,
            tracker,
            [node.consequence]
            + [alt.block for alt in node.alternatives]
            + [node.default],
            implicit_empty_branch=node.default is None,
        )
    elif isinstance(node, CaseNode):
        blocks = [
            child.block if isinstance(child, MultiExpressionBlockNode) else child
            for child in node.blocks
        ]
        _analyze_branches(
            node,
            tracker,
            blocks,
            implicit_empty_branch=not any(
                isinstance(child, BlockNode) for child in node.blocks
            ),
        )
    elif isinstance(node, ForNode):
        _analyze_balanced(node, tracker, node.block)
        if node.default is not None:
            _analyze_balanced(node, tracker, node.default)
    elif isinstance(node, (TablerowNode, IfChangedNode)):
        _analyze_balanced(node, tracker, node.block)
    elif isinstance(node, CaptureNode):
        # A capture block is a self-contained fragment. It is rendered later,
        # wherever its name is output, so it must be internally balanced.
        inner = HTMLContextTracker()
        _analyze_block(node.block, inner)
        if not inner.at_text():
            raise HTMLContextError(
                f"unbalanced HTML in 'capture' block: the block must end in "
                f"HTML text context (left the parser in {inner.describe()})",
                token=node.token,
            )
    elif isinstance(node, (IncludeNode, RenderNode)):
        # Partials are verified to be self-contained fragments (they start and
        # end in HTML text context), so they can only be included from HTML
        # text context.
        if not tracker.at_text():
            raise HTMLContextError(
                f"{node.token.value!r} is only allowed in HTML text context, "
                f"not in {tracker.describe()}",
                token=node.token,
            )
    elif isinstance(node, LiquidNode):
        _analyze_block(node.block, tracker)
    elif isinstance(node, _NO_OUTPUT):
        pass
    else:
        raise HTMLContextError(
            f"tag {node.token.value!r} is not supported when contextual "
            f"autoescape is enabled",
            token=node.token,
        )
