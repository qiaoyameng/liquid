"""Built-in loop expressions."""

from __future__ import annotations

from collections import deque
from itertools import islice
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterator
from typing import Mapping
from typing import Sequence
from typing import Union
from typing import cast

from liquid import Mode
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import LiquidValueError
from liquid.expression import Expression
from liquid.limits import to_int
from liquid.sequence import is_lazy_sequence
from liquid.sequence import liquid_length
from liquid.token import TOKEN_ASSIGN
from liquid.token import TOKEN_COLON
from liquid.token import TOKEN_COLS
from liquid.token import TOKEN_COMMA
from liquid.token import TOKEN_CONTINUE
from liquid.token import TOKEN_EOF
from liquid.token import TOKEN_IN
from liquid.token import TOKEN_LIMIT
from liquid.token import TOKEN_OFFSET
from liquid.token import TOKEN_REVERSED

from .primitive import StringLiteral
from .primitive import parse_identifier
from .primitive import parse_primitive

if TYPE_CHECKING:
    from liquid import Environment
    from liquid import RenderContext
    from liquid import TokenStream
    from liquid.token import Token


class _LazyLoopState:
    __slots__ = ("source", "iterator", "position", "buffer")

    def __init__(self, source: object, iterator: Iterator[Any]) -> None:
        self.source = source
        self.iterator = iterator
        self.position = 0
        self.buffer: deque[Any] = deque()


class _LookaheadIterator:
    __slots__ = ("_state", "_iterator", "start", "size", "_exhausted")

    def __init__(
        self,
        iterator: Iterator[object],
        state: _LazyLoopState,
        start: int,
        size: int | None,
    ) -> None:
        self._state = state
        self._iterator = iterator
        self.start = start
        self.size = size
        self._exhausted = False

    def __iter__(self) -> _LookaheadIterator:
        return self

    def __next__(self) -> object:
        if self.size is not None and self._state.position - self.start >= self.size:
            raise StopIteration
        if self._exhausted and not self._state.buffer:
            raise StopIteration
        item = next(self._iterator)
        self._state.position += 1
        return item

    def is_last(self) -> bool:
        """Return `True` if the next buffered record ends this window."""
        yielded = self._state.position - self.start
        if self.size is not None:
            if yielded == self.size:
                return True

        if self._state.buffer:
            return False

        try:
            self._state.buffer.append(next(self._state.iterator))
        except StopIteration:
            self._exhausted = True
            return True

        return False


class _PeekableIterator:
    __slots__ = ("_state",)

    def __init__(self, state: _LazyLoopState) -> None:
        self._state = state

    def __iter__(self) -> _PeekableIterator:
        return self

    def __next__(self) -> object:
        if self._state.buffer:
            return self._state.buffer.popleft()
        return next(self._state.iterator)


class _PositionIterator:
    __slots__ = ("_state",)

    def __init__(self, state: _LazyLoopState) -> None:
        self._state = state

    def __iter__(self) -> _PositionIterator:
        return self

    def __next__(self) -> object:
        item = next(_PeekableIterator(self._state))
        self._state.position += 1
        return item


class _ReversedWindowIterator:
    __slots__ = ("_iterator",)

    def __init__(self, items: list[Any]) -> None:
        self._iterator = reversed(items)

    def __iter__(self) -> _ReversedWindowIterator:
        return self

    def __next__(self) -> object:
        return next(self._iterator)


class LoopExpression(Expression):
    """An expression for the `for` and `tablerow` tag."""

    __slots__ = ("identifier", "iterable", "limit", "offset", "reversed", "cols")

    def __init__(
        self,
        token: Token,
        identifier: str,
        iterable: Expression,
        *,
        limit: Expression | None,
        offset: Expression | None,
        reversed_: bool,
        cols: Expression | None,
    ) -> None:
        super().__init__(token)
        self.identifier = identifier
        self.iterable = iterable
        self.limit = limit
        self.offset = offset
        self.reversed = reversed_
        self.cols = cols

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, LoopExpression)
            and self.identifier == other.identifier
            and self.iterable == other.iterable
            and self.limit == other.limit
            and self.offset == other.offset
            and self.cols == other.cols
            and self.reversed == other.reversed
        )

    def __str__(self) -> str:
        buf = [f"{self.identifier} in", str(self.iterable)]

        if self.limit is not None:
            buf.append(f"limit:{self.limit}")

        if self.offset is not None:
            buf.append(f"offset:{self.offset}")

        if self.cols is not None:
            buf.append(f"cols:{self.cols}")

        if self.reversed:
            buf.append("reversed")

        return " ".join(buf)

    def _to_iter(
        self, obj: object, context: RenderContext
    ) -> tuple[Iterator[Any], int | None]:
        if isinstance(obj, Mapping):
            return iter(obj.items()), len(obj)
        if isinstance(obj, range):
            try:
                return iter(obj), len(obj)
            except OverflowError as err:
                raise LiquidValueError(
                    "range literal length limit reached", token=None
                ) from err
        if isinstance(obj, str) and not context.env.string_sequences:
            return (iter([]), 0) if not obj else (iter([obj]), 1)
        if isinstance(obj, Sequence):
            return iter(obj), len(obj)

        return iter([]), 0

    def _lazy_length(self, obj: object) -> int | None:
        return liquid_length(obj)

    def _lazy_state(self, obj: object, context: RenderContext) -> _LazyLoopState:
        states = context.tag_namespace.setdefault("lazy_stopindex", {})
        state_entry = states.get(id(obj))
        state = state_entry[1] if state_entry is not None else None

        if state is not None:
            return state

        iterator = iter(cast(Any, obj))
        state = _LazyLoopState(obj, iterator)
        states[id(obj)] = (obj, state)
        return state

    def _skip_lazy(
        self, state: _LazyLoopState, start: int
    ) -> None:
        if state.position < start:
            count = start - state.position
            list(islice(_PositionIterator(state), count))

    def _slice_lazy(
        self,
        obj: object,
        context: RenderContext,
        *,
        limit: int | None,
        offset: int | str | None,
    ) -> tuple[Iterator[object], int | None]:
        total = self._lazy_length(cast(Any, obj))

        if total is None and self.reversed:
            raise LiquidTypeError(
                "can't reverse a lazy sequence with unknown length",
                token=self.token,
            )

        if offset == "continue":
            state = self._lazy_state(obj, context)
            start = state.position
        else:
            state = self._lazy_state(obj, context)
            requested_start = max(int(offset or 0), 0)
            start = max(requested_start, state.position)

        if total is not None:
            start_ = min(start, total)

            if self.reversed:
                self._skip_lazy(state, start_)
                if limit is None:
                    length_ = total - start_
                else:
                    length_ = max(min(start_ + max(limit, 0), total) - start_, 0)
                items = list(
                    islice(
                        _PositionIterator(state),
                        length_,
                    )
                )
                return _ReversedWindowIterator(items), length_

            self._skip_lazy(state, start_)
            stop_ = total if limit is None else min(start_ + max(limit, 0), total)
            length_ = max(stop_ - start_, 0)
            iterator = islice(_PositionIterator(state), length_)
            return iterator, length_

        self._skip_lazy(state, start)

        if limit is not None and limit <= 0:
            return iter([]), 0

        if limit is None:
            return (
                _LookaheadIterator(_PeekableIterator(state), state, start, None),
                None,
            )

        return _LookaheadIterator(_PeekableIterator(state), state, start, limit), None

    def _to_int(self, obj: object, *, token: Token) -> int:
        try:
            return to_int(obj)
        except (ValueError, TypeError) as err:
            raise LiquidTypeError(
                f"expected an integer, found {obj.__class__.__name__}",
                token=token,
            ) from err

    def _slice(
        self,
        it: Iterator[object],
        length: int,
        context: RenderContext,
        *,
        limit: int | None,
        offset: int | str | None,
    ) -> tuple[Iterator[object], int]:
        offset_key = f"{self.identifier}-{self.iterable}"

        if offset == "continue":
            start = context.stopindex(offset_key)
        else:
            start = int(offset or 0)

        stop = None if limit is None else limit + start

        start_ = min(max(start, 0), length)
        stop_ = min(stop or length, length)
        length_ = max(stop_ - start_, 0)

        context.stopindex(key=offset_key, index=stop_)
        it = islice(it, start_, stop_)

        if self.reversed:
            return reversed(list(it)), length_
        return it, length_

    def _is_lazy(self, obj: object) -> bool:
        return is_lazy_sequence(obj)

    def evaluate(self, context: RenderContext) -> tuple[Iterator[object], int | None]:
        obj = self.iterable.evaluate(context)
        limit = (
            self._to_int(self.limit.evaluate(context), token=self.limit.token)
            if self.limit
            else None
        )

        if isinstance(self.offset, StringLiteral):
            offset: Union[str, int, None] = self.offset.value
            if offset != "continue":
                offset = self._to_int(offset, token=self.offset.token)
        elif self.offset is None:
            offset = None
        else:
            offset = self._to_int(
                self.offset.evaluate(context), token=self.offset.token
            )

        if self._is_lazy(obj):
            return self._slice_lazy(obj, context, limit=limit, offset=offset)

        it, length = self._to_iter(obj, context)
        return self._slice(it, length, context, limit=limit, offset=offset)

    async def evaluate_async(
        self, context: RenderContext
    ) -> tuple[Iterator[object], int | None]:
        obj = await self.iterable.evaluate_async(context)
        limit = (
            self._to_int(
                await self.limit.evaluate_async(context), token=self.limit.token
            )
            if self.limit
            else None
        )

        if self.offset is None:
            offset: str | int | None = None
        elif isinstance(self.offset, StringLiteral):
            offset = self.offset.evaluate(context)
            if offset != "continue":
                offset = self._to_int(offset, token=self.offset.token)
        else:
            offset = self._to_int(
                await self.offset.evaluate_async(context), token=self.offset.token
            )

        if self._is_lazy(obj):
            return self._slice_lazy(obj, context, limit=limit, offset=offset)

        it, length = self._to_iter(obj, context)
        return self._slice(it, length, context, limit=limit, offset=offset)

    def children(self) -> list[Expression]:
        children = [self.iterable]

        if self.limit is not None:
            children.append(self.limit)

        if self.offset is not None:
            children.append(self.offset)

        if self.cols is not None:
            children.append(self.cols)

        return children

    @staticmethod
    def parse(env: Environment, tokens: TokenStream) -> LoopExpression:
        """Parse a loop expression from _tokens_."""
        token = tokens.current
        identifier = parse_identifier(env, tokens)
        tokens.eat(TOKEN_IN)
        iterable = parse_primitive(env, tokens)

        reversed_ = False
        offset: Expression | None = None
        limit: Expression | None = None
        cols: Expression | None = None

        argument_separators = (
            (TOKEN_COLON, TOKEN_ASSIGN) if env.keyword_assignment else (TOKEN_COLON,)
        )

        # Leading commas are OK
        if tokens.current.kind == TOKEN_COMMA:
            next(tokens)

        while True:
            arg_token = next(tokens)
            kind = arg_token.kind

            if kind == TOKEN_LIMIT:
                tokens.eat_one_of(*argument_separators)
                limit = parse_primitive(env, tokens)
            elif kind == TOKEN_REVERSED:
                reversed_ = True
            elif kind == TOKEN_OFFSET:
                tokens.eat_one_of(*argument_separators)
                offset_token = tokens.current
                if offset_token.kind == TOKEN_CONTINUE:
                    next(tokens)
                    offset = StringLiteral(token=offset_token, value="continue")
                else:
                    offset = parse_primitive(env, tokens)
            elif kind == TOKEN_COLS:
                tokens.eat_one_of(*argument_separators)
                cols = parse_primitive(env, tokens)
            elif kind == TOKEN_COMMA:
                if env.mode == Mode.STRICT and tokens.peek.kind == TOKEN_COMMA:
                    raise LiquidSyntaxError(
                        f"expected 'reversed', 'offset' or 'limit', found {kind}",
                        token=tokens.peek,
                    )
                continue
            elif kind == TOKEN_EOF:
                break
            else:
                raise LiquidSyntaxError(
                    "expected 'reversed', 'offset' or 'limit'",
                    token=arg_token,
                )

        tokens.eat(TOKEN_EOF)
        return LoopExpression(
            token,
            identifier,
            iterable,
            limit=limit,
            offset=offset,
            reversed_=reversed_,
            cols=cols,
        )
