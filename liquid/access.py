"""Field-level access policies for untrusted templates."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING
from collections.abc import Sequence
from typing import Mapping
from typing import Optional
from typing import Tuple
from typing import Union

from .exceptions import AccessDeniedError

if TYPE_CHECKING:
    from .token import Token

Field = Union[str, int]
FieldPath = Tuple[Field, ...]
FieldPathLike = Union[str, int, Sequence[Union[str, int]]]


class AccessDecision(Enum):
    """Whether, and to what extent, a template may access a value."""

    DENY = "deny"
    TRAVERSE = "traverse"
    ALLOW = "allow"


class AccessPolicy:
    """Base class for render-time data access policies."""

    def __init__(
        self,
        *,
        on_denied: str = "undefined",
        denied_message: str = "access to a restricted template variable was denied",
    ) -> None:
        if on_denied not in ("undefined", "raise"):
            raise ValueError("on_denied must be either 'undefined' or 'raise'")
        self.on_denied = on_denied
        self.denied_message = denied_message

    def decide(
        self,
        root: str,
        path: FieldPath,
        *,
        token: Optional[Token] = None,
    ) -> AccessDecision:
        """Return the access decision for `root` and `path`.

        `path` does not include `root`. An empty `path` is a read of the root
        value itself.
        """
        raise NotImplementedError

    def root_enabled(self, root: str) -> bool:
        """Return whether this policy governs the named root."""
        return True

    def allows(
        self,
        root: str,
        path: FieldPath,
        *,
        token: Optional[Token] = None,
    ) -> bool:
        """Return `True` if a value may be read or traversed."""
        return self.decide(root, path, token=token) is not AccessDecision.DENY

    def deny(self, *, token: Optional[Token]) -> object:
        """Raise for a denied access.

        The exception deliberately contains no path or value, since either could
        disclose information about a restricted field.
        """
        raise AccessDeniedError(self.denied_message, token=token)


class PathAccessPolicy(AccessPolicy):
    """Allow access using root names and field paths.

    `*` in a rule matches one field or index. `**` matches zero or more fields.
    A configured descendant path also authorizes traversal through its parent
    values, but does not authorize rendering or passing those parent values to
    a filter boundary.
    """

    def __init__(
        self,
        allowed: Mapping[str, Union[FieldPathLike, Sequence[FieldPathLike]]],
        *,
        on_denied: str = "undefined",
        denied_message: str = "access to a restricted template variable was denied",
    ) -> None:
        super().__init__(on_denied=on_denied, denied_message=denied_message)
        rules: dict[str, tuple[FieldPath, ...]] = {}

        for root, paths in allowed.items():
            if isinstance(paths, (str, int)) or not isinstance(paths, Sequence):
                raw_paths: Sequence[FieldPathLike] = (paths,)
            else:
                raw_paths = paths
            rules[root] = tuple(_path(path) for path in raw_paths)

        self.allowed = rules

    def decide(
        self,
        root: str,
        path: FieldPath,
        *,
        token: Optional[Token] = None,
    ) -> AccessDecision:
        matches = [
            allowed
            for allowed in self.allowed.get(root, ())
            if _pattern_matches(allowed, path)
        ]
        if not matches:
            return AccessDecision.DENY
        exact_match = any(len(allowed) == len(path) for allowed in matches)
        prefix_match = any(
            len(allowed) > len(path) and _pattern_matches(allowed, path)
            for allowed in matches
        )
        if exact_match and not prefix_match:
            return AccessDecision.ALLOW
        return AccessDecision.TRAVERSE

    def root_enabled(self, root: str) -> bool:
        return root in self.allowed


class _AccessBoundary:
    """A traversal-only value that must not cross a filter/output boundary."""

    __slots__ = ("value", "origin", "root")

    def __init__(self, value: object, root: str, origin: FieldPath) -> None:
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "origin", origin)

    def __str__(self) -> str:
        return ""

    def __repr__(self) -> str:
        return "AccessBoundary()"

    def __bool__(self) -> bool:
        return False

    def __getitem__(self, key: object) -> "_AccessBoundary":
        return self

    def __iter__(self) -> "_AccessBoundary":
        return self

    def __next__(self) -> "_AccessBoundary":
        raise StopIteration

    def __liquid__(self) -> None:
        return None


def is_access_boundary(obj: object) -> bool:
    """Return `True` if `obj` is an internal traversal-only boundary."""
    return isinstance(obj, _AccessBoundary)


class _RestrictedSequence(Sequence[object]):
    """A sequence that cannot render its items without an explicit read."""

    __slots__ = ("items", "origin", "root")

    def __init__(
        self,
        items: list[object] | tuple[object, ...],
        root: str | None = None,
        origin: FieldPath = (),
    ):
        object.__setattr__(self, "items", list(items))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "origin", origin)

    def __iter__(self):
        root = object.__getattribute__(self, "root")
        origin = object.__getattribute__(self, "origin")
        items = object.__getattribute__(self, "items")
        if root is None:
            return iter(items)
        return iter(
            _AccessBoundary(item, root, origin + (index,))
            for index, item in enumerate(items)
        )

    def __len__(self) -> int:
        return len(object.__getattribute__(self, "items"))

    def __getitem__(self, index: object) -> object:
        return object.__getattribute__(self, "items")[index]

    def __str__(self) -> str:
        return ""

    def __repr__(self) -> str:
        return "RestrictedSequence()"

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "items"))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _RestrictedSequence):
            return object.__getattribute__(self, "items") == object.__getattribute__(
                other, "items"
            )
        return NotImplemented

    def __contains__(self, item: object) -> bool:
        return item in object.__getattribute__(self, "items")

    def __reversed__(self):
        return reversed(object.__getattribute__(self, "items"))

    def index(self, item: object, *args: object) -> int:
        return object.__getattribute__(self, "items").index(item, *args)

    def count(self, item: object) -> int:
        return object.__getattribute__(self, "items").count(item)


def _path(path: FieldPathLike) -> FieldPath:
    if isinstance(path, str):
        if path in ("", "."):
            return ()
        return tuple(_field(part) for part in _split_path(path))
    if isinstance(path, int):
        return (path,)
    return tuple(_field(segment) for segment in path)


def _field(part: Union[str, int]) -> Field:
    if isinstance(part, int):
        return part
    if part.lstrip("-").isdigit():
        return int(part)
    return part


def _pattern_matches(pattern: FieldPath, path: FieldPath) -> bool:
    return _match(pattern, 0, path, 0)


def _match(pattern: FieldPath, pi: int, path: FieldPath, vi: int) -> bool:
    while pi < len(pattern):
        if pattern[pi] == "**":
            if pi + 1 == len(pattern):
                return True
            for index in range(vi, len(path) + 1):
                if _match(pattern, pi + 1, path, index):
                    return True
            return False

        if vi >= len(path):
            # Matching a prefix is authorization to traverse towards a permitted
            # descendant, not to read the value itself.
            return pi < len(pattern)

        if pattern[pi] != "*" and pattern[pi] != path[vi]:
            return False

        pi += 1
        vi += 1

    return vi >= len(path)


def _split_path(path: str) -> list[str]:
    fields: list[str] = []
    index = 0

    while index < len(path):
        if path[index] == ".":
            index += 1
            continue

        if path[index] == "[":
            close = path.find("]", index)
            if close == -1:
                raise ValueError("invalid field path")
            fields.append(path[index + 1 : close])
            index = close + 1
            continue

        next_dot = path.find(".", index)
        next_bracket = path.find("[", index)
        candidates = [pos for pos in (next_dot, next_bracket) if pos != -1]
        end = min(candidates) if candidates else len(path)
        fields.append(path[index:end])
        index = end

    return [_unquote(field) for field in fields]


def _unquote(field: str) -> str:
    if len(field) >= 2 and field[0] == field[-1] and field[0] in ("'", '"'):
        return field[1:-1]
    return field
