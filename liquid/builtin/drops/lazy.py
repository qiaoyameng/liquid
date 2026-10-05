"""A protocol for lazy, single-pass sequences consumed by the `for` tag.

A backend might expose a record stream that can only be iterated once and
that does not have a known length. Implement :class:`LazyIterable` (and pass
an instance as template data) so that `{% for %}` can consume such a stream
directly, without first materializing it in a `list`.

The protocol:

* `__iter__` returns an iterator over the stream's records starting at its
  current position. The canonical implementation is a factory: each call
  restarts at the first record, and a `for` loop with a numeric `offset`
  skips the leading records itself. A single-pass source that can not
  rewind should provide a fresh `LazyIterable` per request; Liquid only
  iterates each loop's source once and never holds a reference to records
  after they have been rendered.
* `length_hint` returns the number of records if (and only if) it is known
  without iterating the stream. It must return `None` for a source whose
  length is unknown.
* `reversed` is optional. The default signals that reverse iteration is not
  supported; sources that can reverse cheaply may override it.

Length-dependent features behave explicitly when the length is unknown:

* `forloop.first`, `forloop.index` and `forloop.index0` work normally.
* `forloop.last` works using a bounded, one-record lookahead. It never
  drains the stream to discover a length.
* `forloop.length`, `forloop.rindex` and `forloop.rindex0` raise a
  `liquid.exceptions.LiquidTypeError` rather than inventing a number.
* `reversed` with an integer `limit` buffers only the bounded
  `[offset : offset + limit]` window and reverses that. `reversed` without
  a `limit` streams from :meth:`reversed` if the source implements it
  (records arrive last-first; `offset` then skips records from that
  reversed end); it raises `LiquidTypeError` otherwise.

Each loop's pulls have an explainable upper bound. For a loop with
`offset:O` and `limit:L`, at most `O + L + 1` records are pulled (the extra
pull is the one-record lookahead for `last`); `break` can only reduce that
number. When `last` is not used, the bound is `O + L`. Note that a later
loop with `offset: continue` pulls its own skipped prefix again when the
source's `__iter__` restarts from the beginning; that is inherent to the
factory protocol and stays within the per-loop bound above.

`offset: continue` progress is tracked on the render context under a key
made from the loop variable name and iterable expression. The context is
created per call to `render`, so a second, independent render never
inherits the cursor from a previous render. Backends that stream from a
server-side cursor should likewise provide a fresh `LazyIterable` for each
request.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Iterator
from typing import Optional


class LazyIterable(ABC):
    """Base class for lazy, possibly single-pass, iterables.

    Subclasses must implement :meth:`__iter__` to return an iterator over
    records of the underlying stream, starting at the stream's current
    position.
    """

    @abstractmethod
    def __iter__(self) -> Iterator[Any]:
        """Return an iterator over records of this stream."""

    def length_hint(self) -> Optional[int]:
        """Return the number of records, if it is known, otherwise `None`.

        Returning an integer promises that iterating this sequence yields
        exactly that many records. Returning `None` (the default) means the
        length is unknown, so `forloop.length`, `forloop.rindex`,
        `forloop.rindex0` and unbounded `reversed` refuse to operate.
        """
        return None

    def reversed(self) -> Iterator[Any]:
        """Iterate records in reverse order, if this source supports it.

        A single-pass stream with an unknown length generally can not be
        reversed. The default raises `NotImplementedError`, which causes an
        unbounded `reversed` keyword to fail with a `LiquidTypeError`.
        Sources that can reverse cheaply may override this. Reversing with a
        finite `limit` is handled without calling this method.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__!s} does not support reverse iteration"
        )
