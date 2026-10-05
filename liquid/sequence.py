"""Sequence protocols used by Liquid tags and filters."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any
from typing import Iterator
from typing import Optional

from .exceptions import LiquidTypeError


class LazySequence(ABC):
    """A lazily evaluated sequence backed by a one-shot iterator.

    Unlike a `collections.abc.Sequence`, lazy sequences are not required to
    report their length or support random access. Return `None` from
    `__liquid_length__` when the total number of records is unknown. Each
    render gets its own sequence object and cursor state.
    """

    @abstractmethod
    def __iter__(self) -> Iterator[Any]:
        """Return an iterator over the sequence's records."""

    def __liquid_length__(self) -> Optional[int]:
        """Return the total number of records, or `None` if unknown."""
        return None


def is_lazy_sequence(obj: object) -> bool:
    """Return `True` if _obj_ explicitly implements the lazy sequence protocol."""
    return isinstance(obj, LazySequence)


def liquid_length(obj: object) -> Optional[int]:
    """Return the result of a lazy sequence's length hook."""
    method = getattr(obj, "__liquid_length__", None)
    if callable(method):
        length = method()
        if length is None:
            return None
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise LiquidTypeError(
                "lazy sequence length must be a non-negative integer or None",
                token=None,
            )
        return length
    return None
