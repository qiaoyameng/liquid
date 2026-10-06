"""Flow-sensitive static analysis.

Find template variables that can be proven to be _read before they are
guaranteed to have been assigned_. The analysis understands the control-flow
semantics of the built-in ``if``, ``unless``, ``case`` and ``for`` (including
``tablerow``) tags, ``assign``, ``capture``, ``increment`` and ``decrement``,
``break`` and ``continue``, and the scope boundaries of ``include`` and
``render``.

Nothing here evaluates a template's expressions or calls any business logic
provided by application filters or loaders. Partial template names are read
from the syntax tree directly; dynamically named partials are reported as
unknown influences instead of being resolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import TYPE_CHECKING
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Union

from .ast import BlockNode
from .ast import IllegalNode
from .ast import PartialScope
from .builtin.expressions import Path
from .builtin.expressions import StringLiteral
from .span import Span
from .token import TOKEN_TAG

if TYPE_CHECKING:
    from .ast import Node
    from .expression import Expression
    from .template import BoundTemplate


class BranchKind(str, Enum):
    """Kinds of control-flow branches reported for a missing assignment."""

    IF = "if"
    UNLESS = "unless"
    CASE = "case"
    FOR = "for"


@dataclass(frozen=True)
class MissingBranch:
    """A control-flow branch that can lead to a variable not being assigned.

    Attributes:
        kind: The kind of control-flow construct this branch belongs to.
        condition: A string representation of the branch condition. For an
            ``else`` branch this is ``"else"``. For ``case``/``when``
            alternatives the condition is the text following ``when``.
        span: Location of the branch tag (``if``, ``elsif``, ``else``,
            ``when`` or ``for``, for example).
        negated: If True, the branch is taken when `condition` is false. This
            is used for the body of an ``unless`` tag, ``elsif``/``else``
            alternatives and empty ``for`` loops.
    """

    kind: BranchKind
    condition: str
    span: Span
    negated: bool = False

    def __str__(self) -> str:
        if self.condition == "else":
            return "else"
        if self.kind == BranchKind.FOR:
            prefix = f"for {self.condition}"
        elif self.kind == BranchKind.CASE:
            prefix = f"when {self.condition}" if self.condition else "else"
        elif self.kind == BranchKind.UNLESS:
            prefix = f"unless {self.condition}"
        else:
            prefix = f"if {self.condition}"

        return f"not ({prefix})" if self.negated else prefix


@dataclass(frozen=True)
class UnassignedVariable:
    """A variable read that is reachable on a path where it is not assigned.

    Attributes:
        name: The root name of the variable that might be undefined.
        span: Location of the variable read.
        branches: The sequence of control-flow branches that lead to the read
            with `name` unassigned.
    """

    name: str
    span: Span
    branches: tuple[MissingBranch, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class UnknownImpact:
    """A construct the analyzer could not model precisely.

    When an unknown influence is encountered, reports involving variables at
    that point are suppressed so that we never produce false positives.

    Attributes:
        message: Short description of the unknown influence.
        span: Location of the construct.
    """

    message: str
    span: Span


# The maximum number of flow scenarios tracked at once. If exceeded, we stop
# reporting unassigned variables for the remainder of the template to avoid
# path explosion on very wide branch ladders.
_MAX_SCENARIOS = 4096


class _Terminal(Enum):
    NORMAL = 0
    BREAK = 1
    CONTINUE = 2


@dataclass
class _Frame:
    """A scope frame.

    `bindings` are read-only names introduced by the construct (loop variables,
    forloop helpers, `with` arguments, render arguments, ...).

    `locals_` is a writable template local namespace. Only one frame at a time
    has a writable locals namespace; `include` pushes a binding frame over the
    current locals frame, while `render` pushes a frame with a fresh locals
    namespace.
    """

    bindings: frozenset[str]
    locals_: Optional[set[str]]
    # Names possibly assigned in an enclosing loop, keyed by the loop frame.
    # Stored per-frame so shadowing of a loop variable by an assign in the loop
    # body works correctly.
    maybes: Optional[dict[str, list[MissingBranch]]] = None


@dataclass
class _Scenario:
    """One abstract execution path through a sequence of nodes."""

    frames: list[_Frame]
    defs: set[str]
    branches: list[MissingBranch]
    terminal: _Terminal = _Terminal.NORMAL


class _FlowAnalyzer:
    def __init__(
        self,
        template: BoundTemplate,
        global_names: Iterable[str],
        *,
        include_partials: bool,
    ) -> None:
        self.template = template
        self.env = template.env
        self.include_partials = include_partials

        self.unassigned: dict[tuple[str, Span, tuple[MissingBranch, ...]], None] = {}
        self.unknowns: dict[tuple[str, Span], UnknownImpact] = {}

        root_locals: set[str] = set()
        root_frame = _Frame(bindings=frozenset(global_names), locals_=root_locals)
        self.root_frame = root_frame

        scenario = _Scenario(frames=[root_frame], defs=root_locals, branches=[])
        self.scenarios: list[_Scenario] = [scenario]
        self.exploded = False

        # Partial templates already visited, keyed by template name and scope
        # kind, so recursive includes terminate.
        self._seen_partials: set[tuple[str, str]] = set()

    # -- public entry point -------------------------------------------------

    def analyze(self, nodes: Sequence[Node], template_name: str) -> None:
        self._visit_nodes(nodes, template_name)

    # -- scenario helpers ---------------------------------------------------

    def _mark_unknown(self, message: str, span: Span) -> None:
        key = (message, span)
        if key not in self.unknowns:
            self.unknowns[key] = UnknownImpact(message, span)

    def _is_bound(self, scenario: _Scenario, name: str) -> bool:
        return any(name in frame.bindings for frame in reversed(scenario.frames))

    def _is_local(self, scenario: _Scenario, name: str) -> bool:
        return any(
            frame.locals_ is not None and name in frame.locals_
            for frame in reversed(scenario.frames)
        )

    def _maybe_info(
        self, scenario: _Scenario, name: str
    ) -> Optional[list[MissingBranch]]:
        for frame in reversed(scenario.frames):
            if frame.locals_ is not None:
                # An actual assignment in this locals frame shadows loop
                # maybes from outer frames.
                if name in frame.locals_:
                    return None
            if frame.maybes and name in frame.maybes:
                return frame.maybes[name]
        return None

    def _assign(self, scenarios: Iterable[_Scenario], name: str) -> None:
        for scenario in scenarios:
            if scenario.terminal is not _Terminal.NORMAL:
                continue
            for frame in reversed(scenario.frames):
                if frame.locals_ is not None:
                    frame.locals_.add(name)
                    scenario.defs = frame.locals_
                    break

    def _check_read(self, name: str, span: Span) -> None:
        if self.exploded:
            return

        for scenario in self.scenarios:
            if scenario.terminal is not _Terminal.NORMAL:
                continue
            if name in scenario.defs or self._is_bound(scenario, name):
                continue

            maybes = self._maybe_info(scenario, name)
            if maybes is not None:
                # The name is only assigned on some loop iterations.
                branches = tuple(maybes + scenario.branches)
            else:
                branches = tuple(scenario.branches)

            key = (name, span, branches)
            if key not in self.unassigned:
                self.unassigned[key] = None

    # -- expression walking (no evaluation) ---------------------------------

    def _visit_expressions(
        self, expressions: Iterable[Expression], template_name: str
    ) -> None:
        for expr in expressions:
            self._visit_expression(expr, template_name)

    def _visit_expression(self, expr: Expression, template_name: str) -> None:
        if isinstance(expr, Path):
            root = expr.path[0]
            if isinstance(root, str):
                self._check_read(
                    root, Span(template_name, expr.token.start_index)
                )

        if child_scope := expr.scope():
            # Lambda-style expressions that bind names. None of the built-in
            # expressions currently do this, but honor the protocol.
            names = frozenset(str(ident) for ident in child_scope)
            for scenario in self.scenarios:
                scenario.frames.append(_Frame(bindings=names, locals_=None))
            for child in expr.children():
                self._visit_expression(child, template_name)
            for scenario in self.scenarios:
                scenario.frames.pop()
        else:
            for child in expr.children():
                self._visit_expression(child, template_name)

    # -- node traversal ------------------------------------------------------

    def _visit_nodes(
        self, nodes: Iterable[Node], template_name: str
    ) -> None:
        for node in nodes:
            self._visit_node(node, template_name)
            if self.exploded:
                return

    def _visit_node(self, node: Node, template_name: str) -> None:
        # Expressions attached directly to the node are evaluated before the
        # node runs (conditions, assigned values, ...).
        self._visit_expressions(node.expressions(), template_name)

        if isinstance(node, IllegalNode):
            self._mark_unknown(
                "unrecognized or illegal tag; its effect on variable scope "
                "is unknown",
                Span(template_name, node.token.start_index),
            )
            return

        method = self._dispatch(node)
        if method is not None:
            method(node, template_name)
            return

        if node.token.kind == TOKEN_TAG and self._is_unknown_tag(node):
            self._mark_unknown(
                f"unrecognized custom tag {node.token.value!r}; its effect on "
                "variable scope is unknown",
                Span(template_name, node.token.start_index),
            )
            return

        self._visit_generic(node, template_name)

    def _dispatch(self, node: Node) -> Optional[callable]:  # type: ignore[type-arg]
        # Imports are local to this method to avoid importing optional/extra
        # tags unless they are actually present in the tree.
        from .ast import ConditionalBlockNode
        from .builtin.tags.assign_tag import AssignNode
        from .builtin.tags.capture_tag import CaptureNode
        from .builtin.tags.case_tag import CaseNode
        from .builtin.tags.case_tag import MultiExpressionBlockNode
        from .builtin.tags.for_tag import BreakNode
        from .builtin.tags.for_tag import ContinueNode
        from .builtin.tags.for_tag import ForNode
        from .builtin.tags.if_tag import IfNode
        from .builtin.tags.tablerow_tag import TablerowNode
        from .builtin.tags.unless_tag import UnlessNode

        dispatch = {
            IfNode: self._visit_if,
            UnlessNode: self._visit_if,
            CaseNode: self._visit_case,
            ForNode: self._visit_loop,
            TablerowNode: self._visit_loop,
            BreakNode: self._visit_break,
            ContinueNode: self._visit_break,
            AssignNode: self._visit_assign,
            CaptureNode: self._visit_capture,
            MultiExpressionBlockNode: self._visit_when,
            ConditionalBlockNode: self._visit_conditional_block,
        }

        cls = type(node)
        if cls in dispatch:
            return dispatch[cls]

        name = cls.__name__
        if name == "IncludeNode":
            return self._visit_include
        if name == "RenderNode":
            return self._visit_render
        if name == "WithNode":
            return self._visit_with
        if name == "MacroNode":
            return self._visit_macro
        if name == "CallNode":
            return self._visit_call
        if name == "SnippetNode":
            return self._visit_snippet_definition
        if name == "ExtendsNode":
            return self._visit_extends
        if name == "TranslateNode":
            return self._visit_translate

        return None

    def _is_unknown_tag(self, node: Node) -> bool:
        value = node.token.value
        return value not in self.env.tags

    # -- generic sequence / block nodes ------------------------------------

    def _visit_generic(self, node: Node, template_name: str) -> None:
        children = list(
            node.children(_static_context(self.template), include_partials=False)
        )

        if not children:
            return

        block_scope_names = frozenset(str(i) for i in node.block_scope())

        if block_scope_names:
            for scenario in self.scenarios:
                scenario.frames.append(
                    _Frame(bindings=block_scope_names, locals_=None)
                )
            try:
                self._visit_children(children, template_name)
            finally:
                for scenario in self.scenarios:
                    scenario.frames.pop()
        else:
            self._visit_children(children, template_name)

    def _visit_children(
        self, children: Iterable[Node], template_name: str
    ) -> None:
        for child in children:
            if isinstance(child, BlockNode):
                self._visit_nodes(child.nodes, template_name)
            else:
                self._visit_node(child, template_name)

    # -- assign / capture / increment / decrement --------------------------

    def _visit_assign(self, node: Node, template_name: str) -> None:
        from .builtin.tags.assign_tag import AssignNode

        assert isinstance(node, AssignNode)
        # The value expression has already been visited; only live scenarios
        # observe the assignment.
        self._assign(self.scenarios, str(node.name))

    def _visit_capture(self, node: Node, template_name: str) -> None:
        from .builtin.tags.capture_tag import CaptureNode

        assert isinstance(node, CaptureNode)
        self._visit_nodes(node.block.nodes, template_name)
        self._assign(self.scenarios, str(node.name))

    # -- break / continue ---------------------------------------------------

    def _visit_break(self, node: Node, template_name: str) -> None:
        from .builtin.tags.for_tag import BreakNode
        from .builtin.tags.for_tag import ContinueNode

        terminal = (
            _Terminal.BREAK
            if isinstance(node, BreakNode)
            else _Terminal.CONTINUE
        )
        for scenario in self.scenarios:
            if scenario.terminal is _Terminal.NORMAL:
                scenario.terminal = terminal

    # -- if / unless ---------------------------------------------------------

    def _visit_if(self, node: Node, template_name: str) -> None:
        from .builtin.tags.if_tag import IfNode
        from .builtin.tags.unless_tag import UnlessNode

        kind = BranchKind.UNLESS if isinstance(node, UnlessNode) else BranchKind.IF

        incoming = self.scenarios
        token = node.token

        # Consequence
        taken_branch = MissingBranch(
            kind=kind,
            condition=str(node.condition),
            span=Span(template_name, token.start_index),
            negated=kind is BranchKind.UNLESS,
        )
        not_taken = MissingBranch(
            kind=kind,
            condition=str(node.condition),
            span=Span(template_name, token.start_index),
            negated=kind is BranchKind.IF,
        )

        taken_scenarios = self._fork(incoming, taken_branch)
        self.scenarios = taken_scenarios
        self._visit_nodes(node.consequence.nodes, template_name)
        taken_out = self.scenarios

        # elsif / else alternatives
        fallthrough = self._fork(incoming, not_taken)
        merged: list[_Scenario] = list(taken_out)

        for alt in node.alternatives:
            alt_taken = MissingBranch(
                kind=kind,
                condition=str(alt.expression),
                span=Span(template_name, alt.token.start_index),
            )
            alt_not_taken = MissingBranch(
                kind=kind,
                condition=str(alt.expression),
                span=Span(template_name, alt.token.start_index),
                negated=True,
            )

            alt_scenarios = self._fork(fallthrough, alt_taken)
            self.scenarios = alt_scenarios
            self._visit_nodes(alt.block.nodes, template_name)
            merged.extend(self.scenarios)
            fallthrough = self._fork(fallthrough, alt_not_taken)

        if node.default is not None:
            else_branch = MissingBranch(
                kind=kind,
                condition="else",
                span=Span(template_name, node.default.token.start_index),
            )
            else_scenarios = self._fork(fallthrough, else_branch)
            self.scenarios = else_scenarios
            self._visit_nodes(node.default.nodes, template_name)
            merged.extend(self.scenarios)
        else:
            # No else: the condition failing on every branch skips the tag.
            merged.extend(fallthrough)

        self._finish_branch(incoming, merged)

    def _visit_conditional_block(
        self, node: Node, template_name: str
    ) -> None:
        # Standalone conditional blocks should only appear as children of an
        # if/unless node, which handles them. If we meet one elsewhere, treat
        # its block as unconditional.
        block = getattr(node, "block", None)
        if block is not None:
            self._visit_nodes(block.nodes, template_name)

    # -- case / when --------------------------------------------------------

    def _visit_case(self, node: Node, template_name: str) -> None:
        from .builtin.tags.case_tag import MultiExpressionBlockNode

        incoming = self.scenarios

        whens = [b for b in node.blocks if isinstance(b, MultiExpressionBlockNode)]
        defaults = [
            b for b in node.blocks if not isinstance(b, MultiExpressionBlockNode)
        ]

        no_match_prefix: list[MissingBranch] = []
        merged: list[_Scenario] = []
        eligible = incoming

        for when in whens:
            condition = str(when.expression)
            match_branch = MissingBranch(
                kind=BranchKind.CASE,
                condition=condition,
                span=Span(template_name, when.token.start_index),
            )
            no_match_branch = MissingBranch(
                kind=BranchKind.CASE,
                condition=condition,
                span=Span(template_name, when.token.start_index),
                negated=True,
            )

            matched = self._fork(eligible, match_branch)
            self.scenarios = matched
            self._visit_nodes(when.block.nodes, template_name)
            merged.extend(self.scenarios)

            eligible = self._fork(eligible, no_match_branch)
            no_match_prefix.append(no_match_branch)

        if defaults:
            for default in defaults:
                else_branch = MissingBranch(
                    kind=BranchKind.CASE,
                    condition="else",
                    span=Span(template_name, default.token.start_index),
                )
                else_scenarios = self._fork(eligible, else_branch)
                self.scenarios = else_scenarios
                self._visit_nodes(default.nodes, template_name)
                merged.extend(self.scenarios)
        else:
            merged.extend(eligible)

        self._finish_branch(incoming, merged)

    def _visit_when(self, node: Node, template_name: str) -> None:
        # Handled by _visit_case; defensively treat the block as unconditional
        # if a `when` appears elsewhere.
        self._visit_nodes(node.block.nodes, template_name)

    # -- for / tablerow ------------------------------------------------------

    def _visit_loop(self, node: Node, template_name: str) -> None:
        kind = BranchKind.FOR

        incoming = self.scenarios

        # The iterable/limit/offset expressions are evaluated with the outer
        # scope, which _visit_node already did.

        loop_vars = frozenset(str(i) for i in node.block_scope())
        condition = str(node.expression)
        loop_span = Span(template_name, node.token.start_index)
        empty_branch = MissingBranch(
            kind=kind, condition=condition, span=loop_span, negated=True
        )

        # Find every name assigned anywhere in the loop body. After the loop
        # such a name is only "maybe" assigned, because the iterable could be
        # empty.
        body_writes = _collect_assignments(node.block)

        # Scenario where the loop body never runs.
        zero_scenarios = self._fork(incoming, empty_branch)

        for scenario in zero_scenarios:
            maybes_frame = self._locals_frame(scenario)
            if maybes_frame.maybes is None:
                maybes_frame.maybes = {}
            for name in body_writes:
                maybes_frame.maybes.setdefault(name, []).append(empty_branch)

        # Analyze the loop body once per iteration, with loop variables bound.
        body_scenarios = self._clone(incoming)
        for scenario in body_scenarios:
            scenario.frames.append(
                _Frame(bindings=loop_vars, locals_=None)
            )

        self.scenarios = body_scenarios
        self._visit_nodes(node.block.nodes, template_name)

        # After one or more iterations, assignments made on every live exit
        # path still can't be treated as definite because the body's own
        # branches might have skipped them -- _finish_branch already accounts
        # for that; but the empty-iterable possibility applies to all writes.
        for scenario in self.scenarios:
            scenario.frames.pop()
            if scenario.terminal is _Terminal.CONTINUE:
                scenario.terminal = _Terminal.NORMAL
            if scenario.terminal is _Terminal.NORMAL:
                maybes_frame = self._locals_frame(scenario)
                if maybes_frame.maybes is None:
                    maybes_frame.maybes = {}
                for name in body_writes:
                    maybes_frame.maybes.setdefault(name, []).append(empty_branch)

        # {% else %} block of a for loop runs only when the iterable is empty.
        if node.default is not None:
            self.scenarios = zero_scenarios
            self._visit_nodes(node.default.nodes, template_name)
            else_out = self.scenarios
        else:
            else_out = zero_scenarios

        merged = [s for s in self.scenarios if False]  # empty list, typed
        # Combine zero-iteration (after else block) and body exits. Break
        # exits are normal fall-through after the loop; continue exits are
        # normal too (the loop completed on that iteration).
        body_out = []
        self.scenarios = body_scenarios
        for scenario in body_scenarios:
            if scenario.terminal in (_Terminal.NORMAL, _Terminal.BREAK,
                                     _Terminal.CONTINUE):
                scenario.terminal = _Terminal.NORMAL
                body_out.append(scenario)

        merged = else_out + body_out
        self._finish_branch(incoming, merged)

    @staticmethod
    def _locals_frame(scenario: _Scenario) -> _Frame:
        for frame in reversed(scenario.frames):
            if frame.locals_ is not None:
                return frame
        raise AssertionError("no locals frame")  # pragma: no cover

    # -- include ------------------------------------------------------------

    def _visit_include(self, node: Node, template_name: str) -> None:
        span = Span(template_name, node.token.start_index)

        # The include tag's own expressions (name, bound variable and keyword
        # arguments) have already been read by _visit_node.

        if not self.include_partials:
            return

        name = self._static_partial_name(node.name)
        if name is None:
            self._mark_unknown(
                "dynamically named include; its assignments and reads could "
                "not be analyzed",
                span,
            )
            return

        partial, partial_name = self._load_partial(name, span)
        if partial is None:
            return

        # include shares the caller's template-local scope. Keyword arguments
        # and a bound variable are pushed as a read-only binding frame.
        bindings = frozenset(self._partial_bindings(node, partial))
        for scenario in self.scenarios:
            scenario.frames.append(_Frame(bindings=bindings, locals_=None))

        seen_key = (partial_name, "include")
        if seen_key in self._seen_partials:
            for scenario in self.scenarios:
                scenario.frames.pop()
            return
        self._seen_partials.add(seen_key)

        self._visit_nodes(partial.nodes, partial_name)
        for scenario in self.scenarios:
            scenario.frames.pop()

    # -- render -------------------------------------------------------------

    def _visit_render(self, node: Node, template_name: str) -> None:
        from .builtin.expressions import Identifier as _Identifier

        span = Span(template_name, node.token.start_index)

        if not self.include_partials:
            return

        name_expr = node.name

        # Inline snippets: `{% render some_variable %}` resolves to a template
        # stored in a local/global variable. We don't resolve it (that would
        # mean executing application code), so treat its content as unknown.
        if isinstance(name_expr, _Identifier):
            self._mark_unknown(
                "inline snippet render; its content could not be analyzed",
                span,
            )
            return

        name = self._static_partial_name(name_expr)
        if name is None:
            self._mark_unknown(
                "dynamically named render; its content could not be analyzed",
                span,
            )
            return

        partial, partial_name = self._load_partial(name, span)
        if partial is None:
            return

        # render is isolated: a fresh template-local scope, with only keyword
        # arguments, the optional bound variable and "forloop" visible in
        # addition to the partial's own globals.
        bindings = frozenset(self._partial_bindings(node, partial))
        if node.var is not None:
            bindings |= {"forloop"}

        partial_globals = set(partial.globals) | set(partial.env.globals)

        saved = self.scenarios
        partial_scenarios = []
        for _ in incoming in saved:
            frame = _Frame(
                bindings=bindings | frozenset(partial_globals),
                locals_=set(),
            )
            partial_scenarios.append(
                _Scenario(
                    frames=[frame],
                    defs=frame.locals_,
                    branches=list(incoming.branches),
                )
            )

        seen_key = (partial_name, "render")
        self.scenarios = partial_scenarios
        if seen_key not in self._seen_partials:
            self._seen_partials.add(seen_key)
            self._visit_nodes(partial.nodes, partial_name)

        # Nothing assigned inside an isolated partial leaks to the caller.
        self.scenarios = saved

    # -- with / macro / call / snippet / extends / translate ----------------

    def _visit_with(self, node: Node, template_name: str) -> None:
        bindings = frozenset(str(i) for i in node.block_scope())
        for scenario in self.scenarios:
            scenario.frames.append(_Frame(bindings=bindings, locals_=None))
        try:
            self._visit_nodes(node.block.nodes, template_name)
        finally:
            for scenario in self.scenarios:
                scenario.frames.pop()

    def _visit_macro(self, node: Node, template_name: str) -> None:
        # A macro definition does not execute its block at definition time.
        # Analyze the block in isolation so that references to parameters and
        # caller-provided globals are checked, but its assignments never leak.
        saved = self.scenarios

        bindings = frozenset(str(i) for i in node.block_scope())
        globals_ = frozenset(self.template.env.globals)
        frame = _Frame(bindings=bindings | globals_, locals_=set())
        self.scenarios = [
            _Scenario(frames=[frame], defs=frame.locals_, branches=[])
        ]
        self._visit_nodes(node.block.nodes, template_name)
        self.scenarios = saved

    def _visit_call(self, node: Node, template_name: str) -> None:
        # The call's argument expressions were read by _visit_node. The macro
        # body executes in an isolated scope bound to the call's arguments;
        # model it if the macro is defined in this template.
        macro = self._macros.get(str(node.name)) if hasattr(self, "_macros") else None
        if macro is None:
            self._mark_unknown(
                f"call to macro {str(node.name)!r}; its definition could not "
                "be found statically",
                Span(template_name, node.token.start_index),
            )

    def _visit_snippet_definition(
        self, node: Node, template_name: str
    ) -> None:
        # {% snippet %} binds a template to a name without executing it.
        self._assign(self.scenarios, str(node.name))

    def _visit_extends(self, node: Node, template_name: str) -> None:
        self._mark_unknown(
            "template inheritance; the effect of the parent template could "
            "not be analyzed",
            Span(template_name, node.token.start_index),
        )

    def _visit_translate(self, node: Node, template_name: str) -> None:
        # Keyword arguments are bound for both the singular and plural
        # message blocks. Exactly one of the blocks is rendered, depending on
        # the plural count -- treat both like conditional branches.
        bindings = frozenset(str(i) for i in node.block_scope())
        incoming = self.scenarios

        singular = node.singular_block.block
        plural = node.plural_block.block if node.plural_block else None

        for scenario in incoming:
            scenario.frames.append(_Frame(bindings=bindings, locals_=None))
        self.scenarios = self._clone(incoming)
        # bindings were conceptually pushed on incoming; redo cleanly:
        for scenario in self.scenarios:
            scenario.frames.pop()

        singular_scenarios = self._clone(incoming)
        for scenario in singular_scenarios:
            scenario.frames.append(_Frame(bindings=bindings, locals_=None))
        self.scenarios = singular_scenarios
        self._visit_nodes(singular.nodes, template_name)
        merged = list(self.scenarios)

        if plural is not None:
            plural_scenarios = self._clone(incoming)
            for scenario in plural_scenarios:
                scenario.frames.append(_Frame(bindings=bindings, locals_=None))
            self.scenarios = plural_scenarios
            self._visit_nodes(plural.nodes, template_name)
            merged.extend(self.scenarios)

        self._finish_branch(incoming, merged)

    # -- partial helpers ----------------------------------------------------

    def _static_partial_name(
        self, expression: Expression
    ) -> Optional[str]:
        if isinstance(expression, StringLiteral):
            return expression.value
        return None

    def _partial_bindings(
        self, node: Node, partial: BoundTemplate
    ) -> Iterable[str]:
        from .builtin.expressions import KeywordArgument

        for arg in getattr(node, "args", []):
            if isinstance(arg, KeywordArgument):
                yield arg.name

        if getattr(node, "var", None) is not None:
            alias = getattr(node, "alias", None)
            if alias is not None:
                yield str(alias)
            else:
                yield partial.name.split(".")[0]

    def _load_partial(
        self, name: str, span: Span
    ) -> tuple[Optional[BoundTemplate], str]:
        # Only invoke the loader (which parses template text, it does not
        # execute template filters or render anything). A missing partial is
        # an unknown influence rather than an analysis error.
        try:
            partial = self.env.get_template(name, context=None)
        except Exception as err:  # noqa: BLE001
            message = type(err).__name__
            self._mark_unknown(
                f"partial template {name!r} could not be loaded ({message})",
                span,
            )
            return None, name
        return partial, partial.name or name

    # -- scenario forking / merging ----------------------------------------

    def _fork(
        self,
        scenarios: Iterable[_Scenario],
        branch: MissingBranch,
    ) -> list[_Scenario]:
        cloned = self._clone(scenarios)
        for scenario in cloned:
            scenario.branches = scenario.branches + [branch]
        return cloned

    @staticmethod
    def _clone(scenarios: Iterable[_Scenario]) -> list[_Scenario]:
        return [
            _Scenario(
                frames=list(scenario.frames),
                defs=scenario.defs,
                branches=list(scenario.branches),
                terminal=scenario.terminal,
            )
            for scenario in scenarios
        ]

    def _finish_branch(
        self,
        incoming: Sequence[_Scenario],
        outgoing: Sequence[_Scenario],
    ) -> None:
        # Deduplicate structurally identical scenarios. Names assigned on only
        # some outgoing paths must be "maybe" on the merged locals frame.
        if len(outgoing) > _MAX_SCENARIOS:
            self.exploded = True
            self.scenarios = list(outgoing)[:_MAX_SCENARIOS]
            return

        locals_frame = self._locals_frame(incoming[0])

        # Group outgoing scenarios by their branch chain and terminal state.
        groups: dict[
            tuple[tuple[MissingBranch, ...], _Terminal], list[_Scenario]
        ] = {}
        for scenario in outgoing:
            if scenario.terminal is not _Terminal.NORMAL:
                key = (tuple(scenario.branches), scenario.terminal)
            else:
                key = (tuple(scenario.branches), _Terminal.NORMAL)
            groups.setdefault(key, []).append(scenario)

        merged: list[_Scenario] = []
        for (branches, terminal), group in groups.items():
            if terminal is not _Terminal.NORMAL or len(group) <= 1:
                for scenario in group:
                    scenario.branches = list(branches)
                    scenario.terminal = terminal
                    merged.append(scenario)
                continue

            # Intersect the assigned names across paths sharing this branch
            # chain, recording names only some paths assign as maybes keyed by
            # this branch chain.
            assigned_sets = []
            for scenario in group:
                frame = self._locals_frame(scenario)
                assigned_sets.append(frame.locals_)

            common = set.intersection(*(set(s) for s in assigned_sets))
            union = set.union(*(set(s) for s in assigned_sets))
            maybe_names = union - common

            representative = group[0]
            # Reset the shared locals frame to names every path defines.
            new_locals = set(common)
            frame = self._locals_frame(representative)
            frame.locals_ = new_locals
            representative.defs = new_locals

            if maybe_names:
                if frame.maybes is None:
                    frame.maybes = {}
                for name in maybe_names:
                    frame.maybes.setdefault(name, []).extend(list(branches))

            representative.branches = list(branches)
            merged.append(representative)

        self.scenarios = merged


# -- module-level helpers ----------------------------------------------------


def _static_context(template: BoundTemplate):
    """Build a throwaway render context for node.children() calls.

    The context is never rendered with and no expression is evaluated through
    it; we pass `include_partials=False` when enumerating children so loader
    and filter logic is never triggered.
    """
    from .context import RenderContext

    return RenderContext(template)


def _collect_assignments(block: BlockNode) -> set[str]:
    """Return names assigned anywhere in a loop body, ignoring nested loops.

    Assignments inside a nested for loop are also only possibly executed, so
    including them as body writes of the outer loop is still sound.
    """
    names: set[str] = set()

    def _visit(nodes: Iterable[Node]) -> None:
        for node in nodes:
            cls = type(node).__name__
            if cls in ("AssignNode", "CaptureNode", "IncrementNode",
                       "DecrementNode", "SnippetNode"):
                name = getattr(node, "name", None)
                if name is not None:
                    names.add(str(name))

            for child in node.children(
                _STATIC_CTX_PLACEHOLDER, include_partials=False
            ):
                if isinstance(child, BlockNode):
                    _visit(child.nodes)
                else:
                    _visit([child])

    _visit(block.nodes)
    return names


_STATIC_CTX_PLACEHOLDER = None


# -- public entry points -----------------------------------------------------


def analyze_flow(
    template: BoundTemplate,
    *,
    globals: Optional[Iterable[str]] = None,  # noqa: A002
    include_partials: bool = True,
) -> tuple[list[UnassignedVariable], list[UnknownImpact]]:
    """Find variables that may be read before assignment in `template`.

    Args:
        template: The parsed template to analyze.
        globals: Names declared by the caller (template/environment globals
            plus render-time context). When `None`, keys of the template's and
            environment's configured globals are used.
        include_partials: If True, statically named included and rendered
            partials are analyzed too. Dynamically named partials are reported
            as unknown influences.

    Returns:
        A tuple of provably unassigned variable reads and unknown influences.
    """
    global_names = set(globals) if globals is not None else set()
    if globals is None:
        global_names.update(template.globals.keys())
        global_names.update(template.env.globals.keys())

    analyzer = _FlowAnalyzer(
        template, global_names, include_partials=include_partials
    )
    analyzer.analyze(list(template.nodes), template.name)

    unassigned = [
        UnassignedVariable(name=name, span=span, branches=branches)
        for (name, span, branches) in analyzer.unassigned
    ]
    unknowns = list(analyzer.unknowns.values())
    return unassigned, unknowns
