"""Static validation of templates rendered with contextual HTML escaping.

The validator walks each template's parse tree while advancing an HTML parser
state machine over literal template text. It proves that:

* every dynamic output statement sits in a supported position (HTML text or a
  quoted attribute value) with a deterministic escaping policy;
* all branches of a conditional (``if``/``unless``/``case``) leave the parser
  in the same structural state, so values after the branch can not land in an
  unexpected context;
* loops, ``{% capture %}`` blocks and other inline block tags are balanced;
* included/rendered snippets are themselves balanced fragments;
* literal URL attributes never use a disallowed protocol.

Anything that can not be proven safe (dynamic tag/attribute names, unquoted
attributes, output inside ``<script>``/``<style>``/...) is rejected at parse
time rather than guessed at render time.
"""

from __future__ import annotations

from typing import Any
from typing import Dict
from typing import Iterable
from typing import Optional
from typing import Set
from typing import TYPE_CHECKING

from .context import HtmlState

if TYPE_CHECKING:
    from liquid.template import BoundTemplate

_CONTENT = "ContentNode"
_OUTPUT = "OutputNode"
_CAPTURE = "CaptureNode"
_IF = "IfNode"
_UNLESS = "UnlessNode"
_CONDITIONAL = "ConditionalBlockNode"
_FOR = "ForNode"
_TABLEROW = "TablerowNode"
_CASE = "CaseNode"
_MULTI = "MultiExpressionBlockNode"
_INCLUDE = "IncludeNode"
_RENDER = "RenderNode"
_LIQUID = "LiquidNode"
_IFCHANGED = "IfChangedNode"
_COMMENT = "CommentNode"
_DOC = "DocNode"


class TemplateValidator:
    """Validate all templates loaded by one environment."""

    def __init__(
        self,
        env: Any,
        *,
        stack: Optional[list[str]] = None,
        done: Optional[dict[int, bool]] = None,
    ) -> None:
        self.env = env
        self._done: Dict[int, bool] = done if done is not None else {}
        self._stack: list[str] = stack if stack is not None else []

    def validate_template(self, template: "BoundTemplate") -> None:
        """Validate _template_ and everything it includes, once."""
        if self._done.get(id(template)):
            return

        name = template.full_name()
        if name in self._stack:
            from .exceptions import ContextualEscapeError

            raise ContextualEscapeError(
                f"recursive template include detected at {name!r}",
                token=None,
                template_name=name,
            )
        self._stack.append(name)
        try:
            state = HtmlState(
                allowed_url_schemes=getattr(
                    self.env, "allowed_url_schemes", None
                )
            )
            self._walk(template.nodes, state, template)
            state.finish()
            self._done[id(template)] = True
        finally:
            self._stack.pop()

    # -- node walking -----------------------------------------------------

    def _walk(
        self,
        nodes: Iterable[Any],
        state: HtmlState,
        template: "BoundTemplate",
    ) -> None:
        for node in nodes:
            self._node(node, state, template)

    def _node(
        self, node: Any, state: HtmlState, template: "BoundTemplate"
    ) -> None:
        kind = type(node).__name__

        if kind == _CONTENT:
            state.feed_literal(node.text, token=node.token)
            return

        if kind == _OUTPUT:
            # Raises if the position is unsupported.
            state.dynamic_policy(token=node.token)
            return

        if kind in (_COMMENT, _DOC):
            # Comments emit no markup and do not affect HTML context.
            return

        if kind == _CAPTURE:
            inner = HtmlState(
                allowed_url_schemes=state.allowed_url_schemes,
            )
            self._walk(node.block.nodes, inner, template)
            inner.finish(token=node.token)
            return

        if kind in (_IF, _UNLESS):
            has_else = node.default is not None
            self._branch(
                [node.consequence]
                + [alt.block for alt in node.alternatives]
                + ([node.default] if node.default is not None else []),
                state,
                template,
                token=node.token,
                has_else=has_else,
            )
            return

        if kind == _CASE:
            branches = []
            has_else = False
            for block in node.blocks:
                if type(block).__name__ == _MULTI:
                    branches.append(block.block)
                else:
                    has_else = True
                    branches.append(block)
            self._branch(
                branches,
                state,
                template,
                token=node.token,
                has_else=has_else,
            )
            return

        if kind in (_FOR, _TABLEROW) and node.default is not None:
            # The loop body and the empty-loop branch must converge.
            self._branch(
                [node.block, node.default],
                state,
                template,
                token=node.token,
                has_else=True,
            )
            return

        if kind in (_INCLUDE, _RENDER):
            self._partial(node, state, template)
            return

        if kind == _IFCHANGED:
            # The block is emitted only when its output changes, so treat it
            # like a conditional without an else: it must return to the same
            # context it started in.
            self._branch(
                [node.block],
                state,
                template,
                token=node.token,
                has_else=False,
            )
            return

        if kind in (_LIQUID, _CONDITIONAL):
            self._walk(node.block.nodes, state, template)
            return

        # Tags that emit no markup or render a single inline block: walk any
        # block-like children conservatively.
        walked = False
        for attr_name in ("block", "consequence"):
            child = getattr(node, attr_name, None)
            if child is not None and hasattr(child, "nodes"):
                self._walk(child.nodes, state, template)
                walked = True
        if not walked and getattr(node, "default", None) is not None:
            self._walk(node.default.nodes, state, template)

    def _branch(
        self,
        blocks: Iterable[Any],
        state: HtmlState,
        template: "BoundTemplate",
        *,
        token: Any,
        has_else: bool,
    ) -> None:
        end_signatures: Set[tuple] = set()
        chosen: Optional[HtmlState] = None
        for block in blocks:
            branch_state = state.clone()
            self._walk(block.nodes, branch_state, template)
            end_signatures.add(branch_state.policy_signature())
            chosen = branch_state

        # If no branch is guaranteed to execute, the "no branch taken" path
        # also leaves the context as it was before the conditional.
        if not has_else:
            end_signatures.add(state.policy_signature())

        if len(end_signatures) > 1:
            from .exceptions import ContextualEscapeError

            raise ContextualEscapeError(
                "conditional branches leave the template in different HTML "
                "contexts",
                token=token,
                template_name=template.full_name(),
            )

        # All taken branches converge. If a branch is guaranteed to execute,
        # continue from its end state; otherwise execution might skip the
        # whole conditional, leaving the context unchanged.
        if chosen is not None and has_else:
            state.copy_from(chosen)

    def _partial(
        self, node: Any, state: HtmlState, template: "BoundTemplate"
    ) -> None:
        from .exceptions import ContextualEscapeError

        if state.signature()[0] != "data":
            raise ContextualEscapeError(
                "included templates can only be used in HTML text content, "
                "not inside a tag or attribute",
                token=node.token,
                template_name=template.full_name(),
            )

        name_expr = node.name
        if type(name_expr).__name__ == "StringLiteral":
            partial_name = str(name_expr.value)
        elif isinstance(getattr(name_expr, "value", None), str):
            partial_name = name_expr.value
        else:
            raise ContextualEscapeError(
                "the name of an included or rendered template must be a "
                "string literal when contextual escaping is enabled",
                token=getattr(name_expr, "token", node.token),
                template_name=template.full_name(),
            )

        try:
            partial = self.env.get_template(partial_name, context=None)
        except ContextualEscapeError:
            raise
        except Exception as err:  # noqa: BLE001
            raise ContextualEscapeError(
                f"could not load template {partial_name!r} for contextual "
                "validation",
                token=node.token,
                template_name=template.full_name(),
            ) from err

        self.validate_template(partial)
        # A validated partial is a balanced fragment; it does not alter the
        # surrounding parser state.
