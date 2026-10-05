"""Built-in loop expressions."""

from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterator
from typing import Mapping
from typing import Optional
from typing import Sequence

from liquid import Mode
from liquid.builtin.drops import LazyIterable
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import LiquidValueError
from liquid.expression import Expression
from liquid.limits import to_int
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

    @property
    def stopindex_key(self) -> str:
        """The key used to track `offset: continue` progress in a render."""
        return f"{self.identifier}-{self.iterable}"

    def _to_iter(
        self, obj: object, context: RenderContext
    ) -> tuple[Any, Iterator[Any], Optional[int]]:
        if isinstance(obj, LazyIterable):
            return obj, iter(obj), obj.length_hint()
        if isinstance(obj, Mapping):
            return obj, iter(obj.items()), len(obj)
        if isinstance(obj, range):
            try:
                return obj, iter(obj), len(obj)
            except OverflowError as err:
                raise LiquidValueError(
                    "range literal length limit reached", token=None
                ) from err
        if isinstance(obj, str) and not context.env.string_sequences:
            return obj, iter([] if not obj else [obj]), 0 if not obj else 1
        if isinstance(obj, Sequence):
            return obj, iter(obj), len(obj)

        return obj, iter([]), 0

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
        if offset == "continue":
            start = context.stopindex(self.stopindex_key)
        else:
            start = int(offset or 0)

        stop = None if limit is None else limit + start

        start_ = min(max(start, 0), length)
        stop_ = min(stop or length, length)
        length_ = max(stop_ - start_, 0)

        context.stopindex(key=self.stopindex_key, index=stop_)
        it = islice(it, start_, stop_)

        if self.reversed:
            return reversed(list(it)), length_
        return it, length_

    def _slice_lazy(
        self,
        source: LazyIterable,
        it: Iterator[object],
        context: RenderContext,
        *,
        limit: int | None,
        offset: int | str | None,
    ) -> tuple[LazyLoopIterator, None]:
        if offset == "continue":
            start = context.stopindex(self.stopindex_key)
        else:
            start = int(offset or 0)

        if start < 0:
            start = 0

        window = LazyLoopIterator(
            source,
            it,
            start=start,
            limit=max(limit, 0) if isinstance(limit, int) else None,
            reversed_=self.reversed,
            token=self.token,
        )
        return window, None

    def evaluate(
        self, context: RenderContext
    ) -> tuple[Iterator[object], Optional[int]]:
        source, it, length = self._to_iter(
            self.iterable.evaluate(context), context
        )
        limit = (
            self._to_int(self.limit.evaluate(context), token=self.limit.token)
            if self.limit
            else None
        )

        if self.offset is None:
            offset = None
        else:
            offset_value = self.offset.evaluate(context)
            if isinstance(self.offset, StringLiteral):
                offset = (
                    "continue"
                    if offset_value == "continue"
                    else self._to_int(offset_value, token=self.offset.token)
                )
            else:
                offset = self._to_int(offset_value, token=self.offset.token)

        if length is None:
            return self._slice_lazy(
                source, it, context, limit=limit, offset=offset
            )
        return self._slice(it, length, context, limit=limit, offset=offset)

    async def evaluate_async(
        self,
        context: RenderContext,
    ) -> tuple[Iterator[object], Optional[int]]:
        source, it, length = self._to_iter(
            await self.iterable.evaluate_async(context), context
        )
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

        if length is None:
            return self._slice_lazy(
                source, it, context, limit=limit, offset=offset
            )
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


_MISSING = object()


class _CountingIterator(Iterator[Any]):
    """Wrap an iterator and count records pulled from it."""

    __slots__ = ("_it", "fetched", "exhausted")

    def __init__(self, it: Iterator[Any]) -> None:
        self._it = it
        self.fetched = 0
        self.exhausted = False

    def __next__(self) -> Any:
        try:
            item = next(self._it)
        except StopIteration:
            self.exhausted = True
            raise
        self.fetched += 1
        return item


class LazyLoopIterator(Iterator[Any]):
    """A bounded, single-pass window over a lazy iterable.

    `limit` and `offset` are applied without buffering records. The current
    record is considered the last when either the finite window is full or a
    one-record lookahead finds the underlying stream exhausted, so `last`
    never drains the stream to compute a length.

    The number of records fetched from the underlying stream, including
    skipped and lookahead records, is available from :attr:`fetched`; the
    number of records yielded from the window from :attr:`rendered`. These
    power `offset: continue` progress within a single render.
    """

    __slots__ = (
        "_it",
        "_counter",
        "_start",
        "_token",
        "_rendered",
        "_buffered",
        "_window_exhausted",
    )

    def __init__(
        self,
        source: LazyIterable,
        it: Iterator[Any],
        *,
        start: int,
        limit: int | None,
        reversed_: bool,
        token: Token,
    ) -> None:
        self._start = start
        self._token = token
        self._rendered = 0
        self._buffered: Any = _MISSING
        self._window_exhausted = False

        if reversed_ and not isinstance(limit, int):
            # An unbounded reverse over an unknown-length stream could only
            # be produced by draining the whole stream. The source may opt in
            # by implementing `reversed()`; otherwise we refuse. When using
            # the source's reverse iterator, `offset` skips records from the
            # reversed (last-record-first) end.
            try:
                rev_it = source.reversed()
            except NotImplementedError as err:
                raise LiquidTypeError(
                    "can't reverse a lazy iterable with an unknown length",
                    token=token,
                ) from err
            counter = _CountingIterator(rev_it)
            window: Iterator[Any] = islice(counter, start, None)
        elif reversed_:
            # Integer limit: take the [start : start + limit] window in
            # source order and reverse only that bounded buffer.
            counter = _CountingIterator(it)
            window = reversed(
                list(islice(islice(counter, start, None), limit))
            )
        else:
            counter = _CountingIterator(it)
            stop = start + limit if isinstance(limit, int) else None
            window = islice(counter, start, stop)

        self._counter = counter
        self._it = window

    def __next__(self) -> Any:
        if self._window_exhausted:
            raise StopIteration

        if self._buffered is not _MISSING:
            item = self._buffered
            self._buffered = _MISSING
        else:
            try:
                item = next(self._it)
            except NotImplementedError as err:
                raise LiquidTypeError(
                    "can't reverse a lazy iterable with an unknown length",
                    token=self._token,
                ) from err

        self._rendered += 1
        return item

    def is_last(self) -> bool:
        """Return True if the current record is the last in the window."""
        if self._window_exhausted:
            return True

        try:
            self._buffered = next(self._it)
        except NotImplementedError as err:
            raise LiquidTypeError(
                "can't reverse a lazy iterable with an unknown length",
                token=self._token,
            ) from err
        except StopIteration:
            self._window_exhausted = True
            return True
        return False

    @property
    def fetched(self) -> int:
        """Number of records fetched after the offset position."""
        return self._counter.fetched

    @property
    def rendered(self) -> int:
        """Number of records yielded from this window so far."""
        return self._rendered

    @property
    def consumed(self) -> int:
        """Absolute source position reached by this loop.

        This is the starting offset plus records actually rendered (records
        skipped for an offset have already been accounted for by an earlier
        loop), so it is the index a subsequent `offset: continue` loop
        starts at. For a source-provided reverse iterator it measures
        progress from the reversed end.
        """
        return self._start + self._rendered
