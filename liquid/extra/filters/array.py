"""Extra array filters."""

from __future__ import annotations

import math
import re
from decimal import Decimal
from operator import getitem
from typing import Any
from typing import Optional
from typing import Sequence
from typing import Union
from typing import TYPE_CHECKING

from liquid.access import AccessDecision
from liquid.filter import array_filter
from liquid.filter import sequence_filter
from liquid.filter import trusted_filter
from liquid.filter import with_context
from liquid.limits import to_int

if TYPE_CHECKING:
    from liquid.context import RenderContext


@trusted_filter
@array_filter
def index(left: Sequence[object], obj: object) -> object:
    """Return the zero-based index of an array item.

    `None` is returned if `obj` is not in `left`.
    """
    try:
        return left.index(obj)
    except ValueError:
        return None


RE_NUMERIC = re.compile(r"-?\d+")


@trusted_filter(field_access=True)
@with_context
@sequence_filter
def sort_numeric(
    left: Sequence[object],
    key: object = None,
    *,
    context: Optional[RenderContext] = None,
) -> list[object]:
    """Return a copy of `left` sorted by numeric values found in `left`'s items."""
    if key:
        def key_func(item: object) -> tuple[Union[int, float, Decimal], ...]:
            if context is None:
                try:
                    return _ints(getitem(item, key))
                except (KeyError, IndexError, TypeError):
                    return (math.inf,)
            if context.allow_item(item, key, token=None) is AccessDecision.DENY:
                return (math.inf,)
            return _ints(context.access_item(item, key, default=None, token=None))

        return sorted(left, key=key_func)
    return sorted(left, key=_ints)


def _ints(obj: object) -> tuple[Union[int, float, Decimal], ...]:
    """Key function for the `sort_numeric` filter."""
    if isinstance(obj, bool):
        # isinstance(False, int) == True
        return (math.inf,)
    if isinstance(obj, (int, float, Decimal)):
        return (obj,)

    ints = tuple(to_int(n) for n in RE_NUMERIC.findall(str(obj)))

    if not ints:
        return (math.inf,)
    return ints
