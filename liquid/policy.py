"""Field-level access policies for tenant-facing templates.

An `AccessPolicy` authorizes reads of template data by data root (the leftmost
identifier in a Liquid path) and field path. Policies are enforced before values
are read, never by redacting rendered output.

Provenance is tracked with `GuardedValue`, a thin wrapper around container-like
objects. Guards follow values through indexes, dynamic keys, loop aliases and
built-in filters such as `map` and `where`, so a denied field stays denied no
matter how the value is reached.

Custom filters receive guarded values by default. A filter that needs raw,
unwrapped objects must be explicitly declared trusted, either with
`Environment.add_filter(name, func, trusted=True)` or the
`liquid.filter.trusted_filter` decorator.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import Iterator
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

from .exceptions import DeniedAccessError
from .undefined import Undefined

if TYPE_CHECKING:
    from .context import RenderContext
    from .token import Token

Segment = Union[str, int]
PathTuple = Tuple[Segment, ...]

RESTRICTED = "[restricted]"


class AccessPolicy:
    """Authorize access to template data by root name and field path.

    Args:
        allow: An optional mapping of root names to iterables of allowed field
            paths. If a root is present, only the listed paths (and traversal of
            their ancestors) are allowed. Roots not listed are governed by
            _default_allow_.
        deny: An optional mapping of root names to iterables of denied field
            paths. Deny rules take precedence over allow rules and apply to
            whole subtrees.
        on_denial: What to do when access is denied. `"error"` raises
            `DeniedAccessError`. `"undefined"` resolves to a
            `RestrictedUndefined` instead.
        default_allow: Whether roots without any allow rules are accessible.
            Defaults to `True`, making the policy opt-in per root.

    Field paths are dot-separated strings. `*` matches any single segment,
    including array indexes, and a lone `*` matches everything below the root.
    For example, `{"users": ["*.name"]}` allows `users[0].name` but not
    `users[0].email`.
    """

    def __init__(
        self,
        *,
        allow: Optional[Mapping[str, Iterable[str]]] = None,
        deny: Optional[Mapping[str, Iterable[str]]] = None,
        on_denial: str = "error",
        default_allow: bool = True,
    ):
        if on_denial not in ("error", "undefined"):
            raise ValueError("on_denial must be 'error' or 'undefined'")

        self.on_denial = on_denial
        self.default_allow = default_allow
        self._allow: dict[str, tuple[PathTuple, ...]] = {
            root: tuple(_parse_pattern(path) for path in paths)
            for root, paths in (allow or {}).items()
        }
        self._deny: dict[str, tuple[PathTuple, ...]] = {
            root: tuple(_parse_pattern(path) for path in paths)
            for root, paths in (deny or {}).items()
        }

    def allows(self, root: str, path: PathTuple) -> bool:
        """Return `True` if reading _path_ below _root_ is allowed."""
        for pattern in self._deny.get(root, ()):
            if _covers(pattern, path):
                return False

        allow = self._allow.get(root)
        if allow is None:
            return self.default_allow

        return any(_covers(p, path) or _reaches(p, path) for p in allow)


class RestrictedUndefined(Undefined):
    """An `Undefined` subclass used when a policy denies access to a value.

    Stringifies to an empty string and never exposes the denied value.
    """


class AccessPolicyState:
    """Per-render state for an `AccessPolicy`.

    A new state is created for every `RenderContext`, so denial records and any
    other mutable policy data are never shared between renders.
    """

    __slots__ = ("policy", "context", "denials")

    def __init__(self, policy: AccessPolicy, context: RenderContext):
        self.policy = policy
        self.context = context
        # Paths denied during this render. Useful for auditing and testing.
        self.denials: list[str] = []

    def allows(self, root: str, path: PathTuple) -> bool:
        """Return `True` if reading _path_ below _root_ is allowed."""
        return self.policy.allows(root, path)

    def deny(self, root: str, path: PathTuple, *, token: Optional[Token]) -> Any:
        """Handle a denied read of _path_ below _root_.

        Raises `DeniedAccessError` or returns a `RestrictedUndefined`, depending
        on the policy's `on_denial` mode. The denied value is never included in
        the exception message or the undefined's hint.
        """
        dotted = format_path(root, path)
        self.denials.append(dotted)
        if self.policy.on_denial == "undefined":
            return RestrictedUndefined(
                dotted,
                hint="access denied by the access policy",
                token=token,
            )
        raise DeniedAccessError(
            f"access to '{dotted}' is denied by the access policy", token=token
        )

    def guard(self, value: Any, root: str, path: PathTuple) -> Any:
        """Wrap _value_ in a `GuardedValue` if it can expose nested fields."""
        if isinstance(value, GuardedValue):
            return value
        if _guardable(value):
            return GuardedValue(value, root, path, self)
        return value

    def get_item(self, obj: Any, key: Any) -> Any:
        """Read `obj[key]` using the render context's item getter."""
        return self.context.get_item(obj, key)

    async def get_item_async(self, obj: Any, key: Any) -> Any:
        """An async version of `get_item`."""
        return await self.context.get_item_async(obj, key)


class GuardedValue:
    """A wrapper carrying access provenance for a resolved value.

    Every read of a nested field is authorized against the wrapped value's
    origin root and path before the underlying object is touched.
    """

    __slots__ = ("_value", "_root", "_path", "_state")

    def __init__(
        self, value: Any, root: str, path: PathTuple, state: AccessPolicyState
    ):
        self._value = value
        self._root = root
        self._path = path
        self._state = state

    @property
    def value(self) -> Any:
        """The wrapped, raw value."""
        return self._value

    @property
    def root(self) -> str:
        """The name of the data root this value was resolved from."""
        return self._root

    @property
    def path(self) -> PathTuple:
        """The field path below `root` this value was resolved from."""
        return self._path

    def __getitem__(self, key: Any) -> Any:
        path = self._path + (key,)
        if not self._state.allows(self._root, path):
            return self._state.deny(self._root, path, token=None)
        child = self._state.get_item(self._value, key)
        return self._state.guard(child, self._root, path)

    async def __getitem_async__(self, key: Any) -> Any:
        path = self._path + (key,)
        if not self._state.allows(self._root, path):
            return self._state.deny(self._root, path, token=None)
        child = await self._state.get_item_async(self._value, key)
        return self._state.guard(child, self._root, path)

    def __iter__(self) -> Iterator[Any]:
        value = self._value
        if isinstance(value, Mapping):
            return iter(value)
        if isinstance(value, (str, bytes)):
            return iter(value)
        try:
            length = len(value)
        except TypeError:
            return iter(value)
        return (self[i] for i in range(length))

    def __liquid_iter__(self) -> tuple[Iterator[Any], int]:
        """Return an iterator of guarded items and a length, for `for` loops."""
        value = self._value
        if isinstance(value, Mapping):
            return ((key, self[key]) for key in value), len(value)
        try:
            length = len(value)
        except TypeError:
            return iter([]), 0
        return (self[i] for i in range(length)), length

    def __len__(self) -> int:
        return len(self._value)

    def __contains__(self, item: object) -> bool:
        return unwrap(item) in self._value

    def __eq__(self, other: object) -> bool:
        return self._value == unwrap(other)

    def __ne__(self, other: object) -> bool:
        return self._value != unwrap(other)

    def __lt__(self, other: object) -> bool:
        return self._value < unwrap(other)

    def __le__(self, other: object) -> bool:
        return self._value <= unwrap(other)

    def __gt__(self, other: object) -> bool:
        return self._value > unwrap(other)

    def __ge__(self, other: object) -> bool:
        return self._value >= unwrap(other)

    def __bool__(self) -> bool:
        return bool(self._value)

    def __hash__(self) -> int:
        return hash(self._value)

    def __liquid__(self) -> Any:
        return self._value

    def _redacted(self, value: Any, path: PathTuple) -> Any:
        state = self._state
        if isinstance(value, Mapping):
            return {
                key: (
                    self._redacted(child, path + (key,))
                    if state.allows(self._root, path + (key,))
                    else RESTRICTED
                )
                for key, child in value.items()
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [
                (
                    self._redacted(child, path + (index,))
                    if state.allows(self._root, path + (index,))
                    else RESTRICTED
                )
                for index, child in enumerate(value)
            ]
        return value

    def __str__(self) -> str:
        value = self._value
        if isinstance(value, Mapping) or (
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))
        ):
            return str(self._redacted(value, self._path))
        return str(value)

    def __repr__(self) -> str:
        return f"GuardedValue({self})"

    # Sequence and mapping helpers used by some built-in filters.

    def index(self, item: Any) -> int:
        """Return the index of _item_ in the wrapped sequence."""
        return self._value.index(unwrap(item))

    def count(self, item: Any) -> int:
        """Return the number of times _item_ appears in the wrapped sequence."""
        return self._value.count(unwrap(item))

    def keys(self) -> Any:
        """Return the wrapped mapping's keys."""
        return self._value.keys()

    def values(self) -> Iterator[Any]:
        """Return an iterator of guarded mapping values."""
        return (self[key] for key in self._value)

    def items(self) -> Iterator[tuple[Any, Any]]:
        """Return an iterator of `(key, guarded value)` pairs."""
        return ((key, self[key]) for key in self._value)

    def get(self, key: Any, default: Any = None) -> Any:
        """Return `self[key]` or _default_ if _key_ is missing."""
        try:
            return self[key]
        except (KeyError, IndexError, TypeError):
            return default


def unwrap(value: Any) -> Any:
    """Return the raw value inside a `GuardedValue`, or _value_ unchanged."""
    while isinstance(value, GuardedValue):
        value = value.value
    return value


def format_path(root: str, path: PathTuple) -> str:
    """Return _root_ and _path_ formatted as a dotted Liquid path."""
    buf = [root]
    for segment in path:
        if isinstance(segment, int):
            buf.append(f"[{segment}]")
        elif segment.isidentifier():
            buf.append(f".{segment}")
        else:
            buf.append(f"[{segment!r}]")
    return "".join(buf)


def _parse_pattern(pattern: str) -> PathTuple:
    if pattern == "*":
        return ("*",)
    segments: list[Segment] = []
    for segment in pattern.split("."):
        if segment == "*":
            segments.append("*")
        elif segment.isdigit():
            segments.append(int(segment))
        else:
            segments.append(segment)
    return tuple(segments)


def _segment_matches(pattern: Segment, segment: Segment) -> bool:
    return pattern == "*" or pattern == segment


def _covers(pattern: PathTuple, path: PathTuple) -> bool:
    """True if _pattern_ matches _path_ or one of _path_'s ancestors."""
    if pattern == ("*",):
        return True
    return len(pattern) <= len(path) and all(
        _segment_matches(p, s) for p, s in zip(pattern, path)
    )


def _reaches(pattern: PathTuple, path: PathTuple) -> bool:
    """True if _path_ is _pattern_ or one of _pattern_'s ancestors."""
    if pattern == ("*",):
        return True
    return len(path) <= len(pattern) and all(
        _segment_matches(p, s) for p, s in zip(pattern, path)
    )


def _guardable(value: Any) -> bool:
    if isinstance(value, (str, bytes, int, float, bool, type(None), Undefined)):
        return False
    return isinstance(value, (Mapping, Sequence)) or hasattr(value, "__getitem__")
