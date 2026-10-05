"""Liquid template render context."""

from __future__ import annotations

import datetime
import itertools
import re
import sys
import warnings
from collections import defaultdict
from contextlib import contextmanager
from functools import partial
from functools import reduce
from io import StringIO
from operator import mul
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Iterator
from typing import Mapping
from typing import MutableMapping
from typing import Optional
from typing import Sequence
from typing import Sized
from typing import TextIO
from typing import Union

from .access import AccessDecision
from .access import AccessPolicy
from .access import FieldPath
from .access import _AccessBoundary
from .access import _RestrictedSequence
from .access import is_access_boundary
from .exceptions import ContextDepthError
from .exceptions import LiquidError
from .exceptions import LocalNamespaceLimitError
from .exceptions import LoopIterationLimitError
from .exceptions import UnknownFilterError
from .exceptions import lookup_warning
from .mode import Mode
from .output import LimitedStringIO
from .undefined import UNDEFINED
from .undefined import RestrictedUndefined
from .undefined import Undefined
from .utils import ReadOnlyChainMap

if TYPE_CHECKING:
    from .builtin.tags.for_tag import ForLoop
    from .builtin.tags.tablerow_tag import TableRow
    from .environment import Environment
    from .template import BoundTemplate
    from .token import Token


class RenderContext:
    """A template render context.

    A new render context is created automatically each time `BoundTemplate.render`
    is called, which includes `globals` set on the bound `liquid.Environment` and
    `liquid.template.BoundTemplate`.
    """

    __slots__ = (
        "_copy_depth",
        "autoescape",
        "access_policy",
        "counters",
        "disabled_tags",
        "env",
        "globals",
        "local_namespace_size_carry",
        "locals",
        "loop_iteration_carry",
        "loops",
        "parent_context",
        "origins",
        "root_aliases",
        "scope",
        "tag_namespace",
        "template",
    )

    def __init__(
        self,
        template: BoundTemplate,
        *,
        globals: Optional[Mapping[str, object]] = None,  # noqa: A002
        disabled_tags: Optional[list[str]] = None,
        copy_depth: int = 0,
        parent_context: Optional[RenderContext] = None,
        loop_iteration_carry: int = 1,
        local_namespace_size_carry: int = 0,
    ):
        self.env: Environment = template.env
        self.template = template

        # A namespace for template local variables. Those that are bound with
        # `assign` or `capture`.
        self.locals: MutableMapping[str, object] = {}

        # A read-only namespace containing globally available variables. Usually
        # passed down from the environment.
        self.globals: Mapping[str, object] = globals or {}

        # A namespace for `increment` and `decrement` counters.
        self.counters: dict[str, int] = {}

        # Namespaces are searched in this order. When a context is extended, the
        # temporary namespace is pushed to the front of this chain.
        self.scope = ReadOnlyChainMap(self.locals, self.globals, builtin, self.counters)

        # A namespace supporting stateful tags. Such as `cycle`, `increment`,
        # `decrement` and `ifchanged`.
        self.tag_namespace: dict[str, Any] = {
            "cycles": {},
            "ifchanged": "",
            "stopindex": {},
            "extends": defaultdict(list),
            "macros": {},
        }

        # As stack of loop objects. Used for populating forloop.parentloop and
        # calculating the effective loop count for the loop iteration resource
        # limit.
        self.loops: list[Union[ForLoop, TableRow]] = []

        # A list of tags names that are disallowed in this context. For example,
        # partial templates rendered using the "render" tag are not allowed to
        # use "include" tags.
        self.disabled_tags: list[str] = disabled_tags or []

        # Indicates if HTML auto-escaping is enabled.
        self.autoescape = self.env.autoescape
        self.access_policy: Optional[AccessPolicy] = self.env.access_policy

        # Count of the number of times `copy` was called in creating this context. This
        # helps us guard against recursive use of the "render" tag, or at least fail
        # gracefully.
        self._copy_depth = copy_depth

        # A reference to a parent render context, if this one has been copied.
        self.parent_context = parent_context

        # A loop iteration count carried over from a parent context, if this one has
        # been copied. This helps us adhere to loop iteration limits in templates
        # rendered with the `render` tag.
        self.loop_iteration_carry = loop_iteration_carry

        # The cumulative number of names in parent render context local namespaces. We
        # use this to adhere to local namespace limits when copying a render context.
        self.local_namespace_size_carry = local_namespace_size_carry

        self.origins: dict[int, list[tuple[str, FieldPath]]] = {}
        self.root_aliases: dict[str, Optional[tuple[str, FieldPath]]] = {}
        self._register_initial_roots(globals)

        if parent_context is not None:
            self._inherit_origins(parent_context)

    def assign(self, key: str, val: Any) -> None:
        """Add or replace the context variable named _key_ with the value _val_."""
        self.locals[key] = val
        self._bind_assigned_origin(key, val)
        if (
            self.env.local_namespace_limit
            and self.get_size_of_locals() > self.env.local_namespace_limit
        ):
            raise LocalNamespaceLimitError("local namespace limit reached", token=None)

    def get_size_of_locals(self) -> int:
        """Return the "size" or a "score" for the current local namespace.

        This is used by the optional local namespace resource limit. Override
        `get_size_of_locals` to customize how the limit is calculated. Be sure
        to consider `self.local_namespace_size_carry` when writing a custom
        implementation of `get_size_of_locals`.

        The default implementation uses `sys.getsizeof()` on each of the local
        namespace's values. It is not a reliable measure of size in bytes.
        """
        if not self.env.local_namespace_limit:
            return 0
        return (
            sum(sys.getsizeof(obj, default=1) for obj in self.locals.values())
            + self.local_namespace_size_carry
        )

    def get(
        self, path: list[object], *, token: Optional[Token], default: object = UNDEFINED
    ) -> object:
        """Resolve and return _path_ in the current scope.

        If _token_ is not None, it will be used to give error messages extra contextual
        information.

        Returns _default_ is the path is not in scope.
        """
        return self._resolve_path(path, token, default, self.get_item)

    async def get_async(
        self,
        path: list[object],
        *,
        token: Optional[Token],
        default: object = UNDEFINED,
    ) -> object:
        """Resolve and return _path_ in the current scope.

        If _token_ is not None, it will be used to give error messages extra contextual
        information.

        Returns _default_ is the path is not in scope.
        """
        return await self._resolve_path_async(path, token, default)

    def _resolve_path(
        self,
        path: list[object],
        token: Optional[Token],
        default: object,
        item_getter: Callable[[object, object], object],
    ) -> object:
        it = iter(path)
        root = next(it)
        segments = list(it)

        if not isinstance(root, str):
            if default == UNDEFINED:
                return self.env.undefined(str(root), token=token)
            return default

        origin = self._root_origin(root)
        obj: object

        if origin is None or self.access_policy is None:
            try:
                obj = self.scope[root]
            except (KeyError, TypeError, IndexError):
                if default == UNDEFINED:
                    hint = f"{root!r} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            current_origin: tuple[Optional[str], FieldPath] = (None, ())
        else:
            root_name, root_path = origin
            decision = self._decide(root_name, root_path, token=token)
            if decision is AccessDecision.DENY:
                return self._denied(
                    root_name, root_path, token=token, default=default
                )
            try:
                obj = self.scope[root]
            except (KeyError, TypeError, IndexError):
                if default == UNDEFINED:
                    hint = f"{root!r} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            self._register_value(obj, root_name, root_path)
            current_origin = (root_name, root_path)
            if not segments:
                if decision is AccessDecision.TRAVERSE:
                    return self._denied(
                        root_name, root_path, token=token, default=default
                    )
                return obj
            if decision is AccessDecision.TRAVERSE:
                obj = _AccessBoundary(obj, root_name, root_path)

        for i, segment in enumerate(segments):
            if is_access_boundary(obj):
                obj = object.__getattribute__(obj, "value")
            if isinstance(obj, _RestrictedSequence):
                obj = object.__getattribute__(obj, "items")

            segment = self._normalize_key(obj, segment)
            root_name = current_origin[0]
            policy_key = self._policy_key(obj, segment)
            next_path = current_origin[1] + (policy_key,)

            if root_name is not None:
                decision = self._decide(root_name, next_path, token=token)
                if decision is AccessDecision.DENY:
                    return self._denied(
                        root_name, next_path, token=token, default=default
                    )

            try:
                obj = item_getter(obj, segment)
            except (KeyError, TypeError):
                if default == UNDEFINED:
                    hint = f"{_segments_str(path[: i + 2])} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            except IndexError:
                if default == UNDEFINED:
                    hint = "index out of range"
                    return self.env.undefined(root, hint=hint, token=token)
                return default

            if root_name is not None:
                self._register_value(obj, root_name, next_path)
                decision = self._decide(root_name, next_path, token=token)
                if i + 1 < len(segments):
                    if decision is AccessDecision.TRAVERSE:
                        obj = _AccessBoundary(obj, root_name, next_path)
                elif decision is AccessDecision.TRAVERSE:
                    obj = _AccessBoundary(obj, root_name, next_path)

            current_origin = (root_name, next_path)

        return obj

    async def _resolve_path_async(
        self,
        path: list[object],
        token: Optional[Token],
        default: object,
    ) -> object:
        async def get_item(obj: object, key: object) -> object:
            return await self.get_item_async(obj, key)

        it = iter(path)
        root = next(it)
        segments = list(it)

        if not isinstance(root, str):
            if default == UNDEFINED:
                return self.env.undefined(str(root), token=token)
            return default

        origin = self._root_origin(root)
        obj: object

        if origin is None or self.access_policy is None:
            try:
                obj = self.scope[root]
            except (KeyError, TypeError, IndexError):
                if default == UNDEFINED:
                    hint = f"{root!r} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            current_origin: tuple[Optional[str], FieldPath] = (None, ())
        else:
            root_name, root_path = origin
            decision = self._decide(root_name, root_path, token=token)
            if decision is AccessDecision.DENY:
                return self._denied(
                    root_name, root_path, token=token, default=default
                )
            try:
                obj = self.scope[root]
            except (KeyError, TypeError, IndexError):
                if default == UNDEFINED:
                    hint = f"{root!r} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            self._register_value(obj, root_name, root_path)
            current_origin = (root_name, root_path)
            if not segments:
                if decision is AccessDecision.TRAVERSE:
                    return self._denied(
                        root_name, root_path, token=token, default=default
                    )
                return obj
            if decision is AccessDecision.TRAVERSE:
                obj = _AccessBoundary(obj, root_name, root_path)

        for i, segment in enumerate(segments):
            if is_access_boundary(obj):
                obj = object.__getattribute__(obj, "value")
            if isinstance(obj, _RestrictedSequence):
                obj = object.__getattribute__(obj, "items")

            segment = self._normalize_key(obj, segment)
            root_name = current_origin[0]
            policy_key = self._policy_key(obj, segment)
            next_path = current_origin[1] + (policy_key,)

            if root_name is not None:
                decision = self._decide(root_name, next_path, token=token)
                if decision is AccessDecision.DENY:
                    return self._denied(
                        root_name, next_path, token=token, default=default
                    )

            try:
                obj = await get_item(obj, segment)
            except (KeyError, TypeError):
                if default == UNDEFINED:
                    hint = f"{_segments_str(path[: i + 2])} is undefined"
                    return self.env.undefined(root, hint=hint, token=token)
                return default
            except IndexError:
                if default == UNDEFINED:
                    hint = "index out of range"
                    return self.env.undefined(root, hint=hint, token=token)
                return default

            if root_name is not None:
                self._register_value(obj, root_name, next_path)
                decision = self._decide(root_name, next_path, token=token)
                if i + 1 < len(segments):
                    if decision is AccessDecision.TRAVERSE:
                        obj = _AccessBoundary(obj, root_name, next_path)
                elif decision is AccessDecision.TRAVERSE:
                    obj = _AccessBoundary(obj, root_name, next_path)

            current_origin = (root_name, next_path)

        return obj

    def resolve(
        self, name: str, *, token: Optional[Token] = None, default: object = UNDEFINED
    ) -> Any:
        """Return the object/value at `name` in the current scope.

        This is like `get`, but does a single, top-level lookup rather than a
        chained lookup from a sequence of keys.
        """
        return self._resolve(name, token=token, default=default)

    def _resolve(
        self, name: str, *, token: Optional[Token], default: object = UNDEFINED
    ) -> Any:
        try:
            value = self.scope[name]
        except KeyError:
            if default == UNDEFINED:
                return self.env.undefined(name, token=token)
            return default

        origin = self._root_origin(name)
        if origin is not None and self.access_policy is not None:
            root_name, root_path = origin
            decision = self._decide(root_name, root_path, token=token)
            if decision is AccessDecision.DENY:
                return self._denied(
                    root_name, root_path, token=token, default=default
                )
            if decision is AccessDecision.TRAVERSE:
                return _AccessBoundary(value, root_name, root_path)

        return value

    def filter(self, name: str, token: Optional[Token]) -> Callable[..., object]:  # noqa: A003
        """Return the filter function with given name."""
        try:
            filter_func = self.env.filters[name]
        except KeyError as err:
            if not self.env.strict_filters:
                return _no_op_filter
            raise UnknownFilterError(f"unknown filter '{name}'", token=token) from err

        kwargs: dict[str, Any] = {}

        if getattr(filter_func, "with_context", False):
            kwargs["context"] = self

        if getattr(filter_func, "with_environment", False):
            kwargs["environment"] = self.env

        trusted = getattr(filter_func, "trusted_filter", False)
        boundary_trusted = getattr(filter_func, "trusted_boundary", False)

        if kwargs:
            if hasattr(filter_func, "filter_async"):
                _filter_func = partial(filter_func, **kwargs)
                _filter_func.filter_async = partial(  # type: ignore
                    filter_func.filter_async,
                    **kwargs,
                )
                resolved_filter = _filter_func
            else:
                resolved_filter = partial(filter_func, **kwargs)
        else:
            resolved_filter = filter_func

        if self.access_policy is not None:
            if boundary_trusted:
                return self._field_filter(resolved_filter)
            if not trusted:
                return self._untrusted_filter(resolved_filter, name, token)
            return self._trusted_filter(resolved_filter, name, token)

        return resolved_filter

    def _field_filter(
        self, func: Callable[..., object]
    ) -> Callable[..., object]:
        def as_filter_value(value: object) -> object:
            if is_access_boundary(value):
                raw = object.__getattribute__(value, "value")
                return (
                    _RestrictedSequence(
                        raw,
                        object.__getattribute__(value, "root"),
                        object.__getattribute__(value, "origin"),
                    )
                    if isinstance(raw, (list, tuple))
                    else raw
                )
            if isinstance(value, _RestrictedSequence):
                return value
            return value

        def guarded(left: object, *args: object, **kwargs: object) -> object:
            left = as_filter_value(left)
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            result = func(left, *args, **kwargs)
            return self.protect_result(result)

        async def guarded_async(left: object, *args: object, **kwargs: object) -> object:
            left = as_filter_value(left)
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            filter_async = getattr(func, "filter_async", None)
            if filter_async is not None:
                result = await filter_async(left, *args, **kwargs)
            else:
                result = func(left, *args, **kwargs)
            return self.protect_result(result)

        guarded.filter_async = guarded_async  # type: ignore[attr-defined]
        return guarded

    def _trusted_filter(
        self, func: Callable[..., object], name: str, token: Optional[Token]
    ) -> Callable[..., object]:
        def guarded(left: object, *args: object, **kwargs: object) -> object:
            if is_access_boundary(left):
                return self._denied(
                    object.__getattribute__(left, "root"),
                    object.__getattribute__(left, "origin"),
                    token=token,
                )
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            return func(left, *args, **kwargs)

        async def guarded_async(left: object, *args: object, **kwargs: object) -> object:
            if is_access_boundary(left):
                return self._denied(
                    object.__getattribute__(left, "root"),
                    object.__getattribute__(left, "origin"),
                    token=token,
                )
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            filter_async = getattr(func, "filter_async", None)
            if filter_async is not None:
                return await filter_async(left, *args, **kwargs)
            return func(left, *args, **kwargs)

        guarded.filter_async = guarded_async  # type: ignore[attr-defined]
        return guarded

    def _untrusted_filter(
        self, func: Callable[..., object], name: str, token: Optional[Token]
    ) -> Callable[..., object]:
        def guarded(left: object, *args: object, **kwargs: object) -> object:
            if is_access_boundary(left):
                return self._denied(
                    object.__getattribute__(left, "root"),
                    object.__getattribute__(left, "origin"),
                    token=token,
                )
            if self._contains_sourced(left) and not isinstance(
                left, (str, int, float, bool, bytes, type(None))
            ):
                return RestrictedUndefined(token=token)
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            return func(left, *args, **kwargs)

        async def guarded_async(left: object, *args: object, **kwargs: object) -> object:
            if is_access_boundary(left):
                return self._denied(
                    object.__getattribute__(left, "root"),
                    object.__getattribute__(left, "origin"),
                    token=token,
                )
            if self._contains_sourced(left) and not isinstance(
                left, (str, int, float, bool, bytes, type(None))
            ):
                return RestrictedUndefined(token=token)
            if self._unsafe_arguments(args, kwargs):
                return RestrictedUndefined(token=token)
            filter_async = getattr(func, "filter_async", None)
            if filter_async is not None:
                return await filter_async(left, *args, **kwargs)
            return func(left, *args, **kwargs)

        guarded.filter_async = guarded_async  # type: ignore[attr-defined]
        return guarded

    def _origin_or_none(self, value: object) -> Optional[tuple[str, FieldPath]]:
        origins = self.origins.get(id(value))
        return origins[0] if origins else None

    def _contains_sourced(self, value: object, depth: int = 0) -> bool:
        if depth > 10:
            return True
        if isinstance(value, _RestrictedSequence):
            return True
        if is_access_boundary(value) or self._origin_or_none(value) is not None:
            return True
        if isinstance(value, (list, tuple)):
            return any(self._contains_sourced(item, depth + 1) for item in value)
        if isinstance(value, Mapping):
            return any(
                self._contains_sourced(key, depth + 1)
                or self._contains_sourced(item, depth + 1)
                for key, item in value.items()
            )
        return False

    def _unsafe_arguments(
        self, args: tuple[object, ...], kwargs: dict[str, object]
    ) -> bool:
        values = (*args, *kwargs.values())
        return any(
            isinstance(value, RestrictedUndefined) or self._contains_sourced(value)
            for value in values
        )

    def get_template(self, name: str) -> BoundTemplate:
        """Load a template from the environment."""
        return self.env.get_template(name)

    async def get_template_async(self, name: str) -> BoundTemplate:
        """Load a template from the environment asynchronously."""
        return await self.env.get_template_async(name)

    def increment(self, name: str) -> int:
        """Increment the named counter and return its value."""
        val: int = self.counters.get(name, 0)
        self.counters[name] = val + 1
        return val

    def decrement(self, name: str) -> int:
        """Decrement the named counter and return its value."""
        val: int = self.counters.get(name, 0) - 1
        self.counters[name] = val
        return val

    def cycle(self, key: object, length: int) -> int:
        """Return the index of the next item in the cycle."""
        namespace: dict[object, int] = self.tag_namespace["cycles"]
        idx = namespace.setdefault(key, 0)
        namespace[key] = (idx + 1) % (length or 1)  # Just in case.
        return idx

    def ifchanged(self, val: str) -> bool:
        """Return True if the `ifchanged` value has changed."""
        if val != self.tag_namespace["ifchanged"]:
            self.tag_namespace["ifchanged"] = val
            return True

        return False

    def stopindex(self, key: str, index: Optional[int] = None) -> int:
        """Set or return the stop index of a for loop."""
        if index is not None:
            self.tag_namespace["stopindex"][key] = index
            return index

        idx: int = self.tag_namespace["stopindex"].get(key, 0)
        return idx

    @contextmanager
    def extend(
        self, namespace: Mapping[str, object], template: Optional[BoundTemplate] = None
    ) -> Iterator[RenderContext]:
        """Extend this context with the given read-only namespace."""
        if self.scope.size() > self.env.context_depth_limit:
            raise ContextDepthError(
                "maximum context depth reached, possible recursive include", token=None
            )

        # Remember the current template so we can restore it upon exiting the
        # context manager.
        _template = self.template
        if template:
            self.template = template

        alias_snapshot = dict(self.root_aliases)
        self.scope.push(namespace)
        self.bind_named_roots(namespace)

        try:
            yield self
        finally:
            if template:
                self.template = _template
            self.scope.pop()
            self.root_aliases.clear()
            self.root_aliases.update(alias_snapshot)

    @contextmanager
    def loop(
        self, namespace: Mapping[str, object], forloop: ForLoop | TableRow
    ) -> Iterator[RenderContext]:
        """Just like `Context.extend`, but keeps track of ForLoop objects too."""
        self.raise_for_loop_limit(forloop.length)
        self.loops.append(forloop)
        with self.extend(namespace) as context:
            try:
                yield context
            finally:
                self.loops.pop()

    def parentloop(self) -> Union[Undefined, object]:
        """Return the last ForLoop object from the loop stack."""
        for loop in reversed(self.loops):
            if hasattr(loop, "parentloop"):
                return loop

        return self.env.undefined("parentloop", token=None)

    def raise_for_loop_limit(self, length: int = 1) -> None:
        """Raise a `LoopIterationLimitError` if loop stack is bigger than the limit."""
        if (
            self.env.loop_iteration_limit
            and reduce(
                mul,
                (loop.length for loop in self.loops),
                length * self.loop_iteration_carry,
            )
            > self.env.loop_iteration_limit
        ):
            raise LoopIterationLimitError("loop iteration limit reached", token=None)

    def copy(
        self,
        namespace: Mapping[str, object],
        disabled_tags: Optional[list[str]] = None,
        carry_loop_iterations: bool = False,
        template: Optional[BoundTemplate] = None,
        block_scope: bool = False,
    ) -> RenderContext:
        """Return a copy of this render context.

        Local variables and other state for stateful tags are not copied.
        """
        if self._copy_depth > self.env.context_depth_limit:
            raise ContextDepthError(
                "maximum context depth reached, possible recursive render", token=None
            )

        if carry_loop_iterations:
            loop_iteration_carry = reduce(
                mul,
                (loop.length for loop in self.loops),
                self.loop_iteration_carry,
            )
        else:
            loop_iteration_carry = 1

        if block_scope:
            ctx = self.__class__(
                template or self.template,
                globals=ReadOnlyChainMap(namespace, self.globals),
                disabled_tags=disabled_tags,
                copy_depth=self._copy_depth + 1,
                parent_context=self,
                loop_iteration_carry=loop_iteration_carry,
                local_namespace_size_carry=self.get_size_of_locals(),
            )
            # This might need to be generalized so the caller can specify which
            # tag namespaces need to be copied.
            ctx.tag_namespace["extends"] = self.tag_namespace["extends"]
        else:
            ctx = self.__class__(
                template or self.template,
                globals=ReadOnlyChainMap(namespace, self.globals),
                disabled_tags=disabled_tags,
                copy_depth=self._copy_depth + 1,
                parent_context=self,
                loop_iteration_carry=loop_iteration_carry,
                local_namespace_size_carry=self.get_size_of_locals(),
            )

        return ctx

    def error(self, exc: LiquidError) -> None:
        """Ignore, raise or convert the given exception to a warning."""
        if self.env.mode == Mode.STRICT:
            raise exc
        if self.env.mode == Mode.WARN:
            warnings.warn(
                str(exc), category=lookup_warning(exc.__class__), stacklevel=2
            )

    def get_buffer(self, buf: Optional[TextIO] = None) -> StringIO:
        """Return a new StringIO object that respects the configured stream limit."""
        if self.env.output_stream_limit is None:
            return StringIO()

        carry = buf.size if isinstance(buf, LimitedStringIO) else 0
        return LimitedStringIO(limit=self.env.output_stream_limit - carry)

    def get_item(self, obj: Any, key: Any) -> Any:  # noqa: PLR0911, PLR0912
        """An item getter used when resolving a Liquid path.

        Override this to change the behavior of `.first`, `.last` and `.size`.
        """
        if hasattr(key, "__liquid__"):
            key = key.__liquid__()

        if key == "size":
            try:
                return obj["size"]
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, Sized):
                    return len(obj)
                raise

        if key == "first":
            try:
                return obj["first"]
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, Mapping) and obj:
                    return next(itertools.islice(obj.items(), 1))
                if isinstance(obj, str) and not self.env.string_first_and_last:
                    raise TypeError("string object has no method 'first'") from None
                if isinstance(obj, Sequence):
                    return obj[0]
                raise

        if key == "last":
            try:
                return obj["last"]
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, str) and not self.env.string_first_and_last:
                    raise TypeError("string object has no method 'last'") from None
                if isinstance(obj, Sequence):
                    return obj[-1]
                raise

        if (
            not self.env.string_sequences
            and isinstance(key, int)
            and isinstance(obj, str)
        ):
            raise TypeError("string object is not subscriptable")

        return obj[key]

    def access_item(
        self,
        obj: object,
        key: object,
        *,
        default: object = None,
        token: Optional[Token] = None,
        register: bool = True,
    ) -> object:
        """Authorize and read `key` from `obj` from a built-in filter."""
        boundary_origin: Optional[tuple[str, FieldPath]] = None
        if is_access_boundary(obj):
            boundary_origin = (
                object.__getattribute__(obj, "root"),
                object.__getattribute__(obj, "origin"),
            )
        obj = self._unwrap_value(obj)

        origin = boundary_origin or self._value_origin(obj, "", ())
        key = self._normalize_key(obj, key)
        policy_key = self._policy_key(obj, key)
        next_path = origin[1] + (policy_key,)

        if origin[0] is not None:
            decision = self._decide(origin[0], next_path, token=token)
            if decision is AccessDecision.DENY:
                if default != UNDEFINED:
                    return default
                return self._denied(origin[0], next_path, token=token)
            if decision is AccessDecision.TRAVERSE and not register:
                return RestrictedUndefined(token=token)

        try:
            value = self.get_item(obj, key)
        except (KeyError, IndexError, TypeError):
            return default

        if register and origin[0] is not None:
            self._register_value(value, origin[0], next_path)
            if self._decide(origin[0], next_path, token=token) in (
                AccessDecision.TRAVERSE,
            ):
                value = _AccessBoundary(value, origin[0], next_path)
        return value

    def allow_item(
        self, obj: object, key: object, *, token: Optional[Token] = None
    ) -> AccessDecision:
        """Authorize a filter item read without performing that read."""
        if is_access_boundary(obj):
            root = object.__getattribute__(obj, "root")
            origin_path = object.__getattribute__(obj, "origin")
            raw = object.__getattribute__(obj, "value")
            key = self._normalize_key(raw, key)
            return self._enforce_decision(
                self._decide(
                root,
                origin_path + (self._policy_key(raw, key),),
                token=token,
                ),
                token,
            )

        obj = self._unwrap_value(obj)
        origin = self._value_origin(obj, "", ())
        if origin[0] is None:
            return AccessDecision.ALLOW
        key = self._normalize_key(obj, key)
        return self._enforce_decision(
            self._decide(
                origin[0],
                origin[1] + (self._policy_key(obj, key),),
                token=token,
            ),
            token,
        )

    def _enforce_decision(
        self, decision: AccessDecision, token: Optional[Token]
    ) -> AccessDecision:
        if (
            decision is AccessDecision.DENY
            and self.access_policy is not None
            and self.access_policy.on_denied == "raise"
        ):
            self.access_policy.deny(token=token)
        return decision

    def protect(self, obj: object) -> object:
        """Return a traversal-only boundary if `obj` is not readable itself."""
        if is_access_boundary(obj):
            return obj
        origin = self._value_origin(obj, "", ())
        if origin[0] is not None and self._decide(
            origin[0], origin[1], token=None
        ) is AccessDecision.TRAVERSE:
            return _AccessBoundary(obj, origin[0], origin[1])
        return obj

    def restrict_sequence(self, items: object) -> object:
        """Return a sequence whose items retain origins but cannot print."""
        if isinstance(items, (list, tuple)):
            return _RestrictedSequence(items)
        return self.protect(items)

    def protect_result(self, result: object) -> object:
        """Protect a filter result while preserving registered item origins."""
        if isinstance(result, _RestrictedSequence):
            raw_items = object.__getattribute__(result, "items")
            root = object.__getattribute__(result, "root")
            origin = object.__getattribute__(result, "origin")
            if root is not None:
                return _RestrictedSequence(raw_items, root, origin)
            return _RestrictedSequence([self.protect(item) for item in raw_items])
        if isinstance(result, (list, tuple)):
            return [self.protect(item) for item in result]
        return self.protect(result)

    def bind_root(self, name: str, value: object) -> None:
        """Bind a namespace root to the source of `value`, if it has one."""
        origin = self._value_origin(value, name, ())
        if origin[0] is not None:
            self.root_aliases[name] = origin
        elif name not in self.root_aliases:
            self.root_aliases[name] = None

    def bind_named_roots(self, namespace: Mapping[str, object]) -> None:
        """Bind sources for all roots in `namespace`."""
        for name, value in namespace.items():
            self.bind_root(name, value)

    def bind_loop_alias(self, name: str, value: object) -> None:
        """Bind a loop variable to the indexed source of the loop collection."""
        origin = self._value_origin(value, "", ())
        if origin[0] is not None:
            self.root_aliases[name] = origin
        else:
            self.root_aliases[name] = None

    def loop_items(
        self, obj: object
    ) -> tuple[Iterator[object], int]:
        """Return an authorized iterator and length for a loop expression."""
        boundary_origin: Optional[tuple[str, FieldPath]] = None
        if is_access_boundary(obj):
            boundary_origin = (
                object.__getattribute__(obj, "root"),
                object.__getattribute__(obj, "origin"),
            )
        obj = self._unwrap_value(obj)

        origin = boundary_origin or self._value_origin(obj, "", ())

        if isinstance(obj, Mapping):
            items: list[object] = []
            for key in obj.keys():
                if origin[0] is None:
                    items.append((key, obj[key]))
                    continue

                key_path = origin[1] + (key,)
                decision = self._decide(origin[0], key_path, token=None)
                if decision is AccessDecision.DENY:
                    items.append(RestrictedUndefined())
                    continue

                value = self.get_item(obj, key)
                self._register_value(value, origin[0], key_path)
                pair = (key, value)
                self._register_value(pair, origin[0], key_path)
                items.append(
                    _AccessBoundary(pair, origin[0], key_path)
                    if decision is AccessDecision.TRAVERSE
                    else pair
                )

            return iter(items), len(obj)

        if isinstance(obj, range):
            try:
                length = len(obj)
            except OverflowError:
                raise
            return iter(obj), length

        if isinstance(obj, str):
            if not self.env.string_sequences:
                return (iter([]), 0) if not obj else (iter([obj]), 1)
            return iter(obj), len(obj)

        if isinstance(obj, Sequence):
            if origin[0] is None:
                return iter(obj), len(obj)

            items = []
            for index, value in enumerate(obj):
                path = origin[1] + (index,)
                decision = self._decide(origin[0], path, token=None)
                if decision is AccessDecision.DENY:
                    items.append(RestrictedUndefined())
                    continue
                self._register_value(value, origin[0], path)
                items.append(
                    _AccessBoundary(value, origin[0], path)
                    if decision is AccessDecision.TRAVERSE
                    else value
                )
            return iter(items), len(obj)

        return iter([]), 0

    def _decide(
        self, root: str, path: FieldPath, *, token: Optional[Token]
    ) -> AccessDecision:
        if self.access_policy is None:
            return AccessDecision.ALLOW
        return self.access_policy.decide(root, path, token=token)

    def _unwrap_value(self, value: object) -> object:
        while is_access_boundary(value):
            value = object.__getattribute__(value, "value")
        while isinstance(value, _RestrictedSequence):
            value = object.__getattribute__(value, "items")
        return value

    def _denied(
        self,
        root: str,
        path: FieldPath,
        *,
        token: Optional[Token],
        default: object = UNDEFINED,
    ) -> object:
        if default != UNDEFINED:
            return default
        assert self.access_policy is not None
        if self.access_policy.on_denied == "raise":
            self.access_policy.deny(token=token)
        return RestrictedUndefined(token=token)

    def is_restricted(self, obj: object) -> bool:
        """Return `True` if `obj` is a placeholder from a policy denial."""
        return isinstance(obj, RestrictedUndefined)

    def output_value(self, value: object, token: Optional[Token]) -> object:
        """Authorize a value immediately before it is rendered."""
        if is_access_boundary(value):
            return self._denied(
                object.__getattribute__(value, "root"),
                object.__getattribute__(value, "origin"),
                token=token,
            )
        return value

    def _normalize_key(self, obj: object, key: object) -> object:
        if hasattr(key, "__liquid__"):
            key = key.__liquid__()
        return key

    def _policy_key(self, obj: object, key: object) -> object:
        if key == "first" and isinstance(obj, Sequence):
            return 0
        if key == "last" and isinstance(obj, Sequence):
            return -1
        return key

    def _register_initial_roots(
        self, namespace: Optional[Mapping[str, object]]
    ) -> None:
        if namespace is None:
            return
        for name in namespace:
            try:
                value = namespace[name]
            except KeyError:
                if self.access_policy is not None and self.access_policy.root_enabled(
                    name
                ):
                    self.root_aliases.setdefault(name, (name, ()))
            else:
                if self.access_policy is not None and self.access_policy.root_enabled(
                    name
                ):
                    self._register_value(value, name, ())
                    self.root_aliases.setdefault(name, (name, ()))
                else:
                    self.root_aliases.setdefault(name, None)

    def _inherit_origins(self, parent: RenderContext) -> None:
        for identity, origins in parent.origins.items():
            self.origins.setdefault(identity, list(origins))

    def _bind_assigned_origin(self, name: str, value: object) -> None:
        origin = self._value_origin(value, "", ())
        if origin[0] is not None:
            self.root_aliases[name] = origin
        else:
            self.root_aliases[name] = None

    def _root_origin(self, name: str) -> Optional[tuple[str, FieldPath]]:
        if name in self.root_aliases:
            return self.root_aliases[name]
        if self.access_policy is None:
            return None
        try:
            value = self.scope[name]
        except (KeyError, TypeError):
            return None
        origin = self._value_origin(value, name, ())
        return origin if origin[0] is not None else None

    def _value_origin(
        self, value: object, fallback_root: str, fallback_path: FieldPath
    ) -> tuple[Optional[str], FieldPath]:
        boundary = is_access_boundary(value)
        if boundary:
            boundary_origin = (
                object.__getattribute__(value, "root"),
                object.__getattribute__(value, "origin"),
            )
            value = object.__getattribute__(value, "value")
        origins = self.origins.get(id(value))
        if origins:
            exact = next(
                (origin for origin in origins if not boundary or origin == boundary_origin),
                origins[0],
            )
            if boundary and exact != boundary_origin:
                return boundary_origin
            return exact
        if fallback_root and self.access_policy is not None:
            return fallback_root, fallback_path
        return None, fallback_path

    def _register_value(
        self, value: object, root: str, path: FieldPath
    ) -> None:
        if isinstance(value, (str, int, float, bool, bytes)) or value is None:
            return
        identity = id(value)
        origin = (root, path)
        origins = self.origins.setdefault(identity, [])
        if origin not in origins:
            origins.append(origin)

    async def get_item_async(self, obj: Any, key: Any) -> Any:  # noqa: PLR0911, PLR0912
        """An async item getter for resolving paths."""

        async def _get_item(obj: Any, key: Any) -> object:
            if hasattr(obj, "__getitem_async__"):
                return await obj.__getitem_async__(key)
            return obj[key]

        if hasattr(key, "__liquid__"):
            key = key.__liquid__()

        if key == "size":
            try:
                return await _get_item(obj, "size")
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, Sized):
                    return len(obj)
                raise

        if key == "first":
            try:
                return await _get_item(obj, "first")
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, Mapping) and obj:
                    return next(itertools.islice(obj.items(), 1))
                if isinstance(obj, str) and not self.env.string_first_and_last:
                    raise TypeError("string object has no method 'first'") from None
                if isinstance(obj, Sequence):
                    return obj[0]
                raise

        if key == "last":
            try:
                return await _get_item(obj, "last")
            except (KeyError, IndexError, TypeError):
                if isinstance(obj, str) and not self.env.string_first_and_last:
                    raise TypeError("string object has no method 'last'") from None
                if isinstance(obj, Sequence):
                    return obj[-1]
                raise

        if (
            not self.env.string_sequences
            and isinstance(key, int)
            and isinstance(obj, str)
        ):
            raise TypeError("string object is not subscriptable")

        return await _get_item(obj, key)


class FutureContext(RenderContext):
    """A render context configured for maximum compatibility with the Ruby liquid."""


class BuiltIn(Mapping[str, object]):
    """Mapping-like object for resolving built-in, dynamic objects."""

    def __contains__(self, item: object) -> bool:
        return item in ("now", "today")

    def __getitem__(self, key: str) -> object:
        if key == "now":
            return datetime.datetime.now()
        if key == "today":
            return datetime.date.today()
        raise KeyError(str(key))

    def __len__(self) -> int:
        return 2

    def __iter__(self) -> Iterator[str]:
        return iter(["now", "today"])


builtin = BuiltIn()

RE_PROPERTY = re.compile(r"[\u0080-\uFFFFa-zA-Z_][\u0080-\uFFFFa-zA-Z0-9_-]*")


def _segments_str(segments: list[object]) -> str:
    it = iter(segments)
    buf = [str(next(it))]
    for segment in it:
        if isinstance(segment, str):
            if RE_PROPERTY.fullmatch(segment):
                buf.append(f".{segment}")
            else:
                buf.append(f"[{segment!r}]")
    return "".join(buf)


def _no_op_filter(left: object, *_args: object, **_kwargs: object) -> object:
    return left
