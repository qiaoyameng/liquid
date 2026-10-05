"""Test cases for lazy, one-shot record streams."""

from __future__ import annotations

from typing import Any
from typing import Iterator

import pytest

from liquid import Environment
from liquid import LazySequence
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import LoopIterationLimitError


class CountingStream(LazySequence):
    """A one-shot stream that records calls to its underlying source."""

    def __init__(self, items: list[Any], *, length: int | None = None):
        self._items = iter(items)
        self.length = length
        self.pulls = 0

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        self.pulls += 1
        return next(self._items)

    def __liquid_length__(self) -> int | None:
        return self.length


@pytest.fixture
def env() -> Environment:
    return Environment()


def test_empty_stream_renders_else_and_pulls_once(env: Environment) -> None:
    stream = CountingStream([])
    template = env.from_string(
        "{% for item in stream %}{{ item }}{% else %}empty{% endfor %}"
    )

    assert template.render(stream=stream) == "empty"
    assert stream.pulls == 1


def test_zero_limit_pulls_no_records(env: Environment) -> None:
    stream = CountingStream([1, 2, 3])
    template = env.from_string(
        "{% for item in stream limit: 0 %}{{ item }}{% else %}empty{% endfor %}"
    )

    assert template.render(stream=stream) == "empty"
    assert stream.pulls == 0


def test_limit_and_last_pull_bound(env: Environment) -> None:
    stream = CountingStream([1, 2, 3, 4, 5])
    template = env.from_string(
        "{% for item in stream limit: 3 %}"
        "{{ item }}:{{ forloop.first }}:{{ forloop.last }};"
        "{% endfor %}"
    )

    assert template.render(stream=stream) == "1:true:false;2:false:false;3:false:true;"
    assert stream.pulls == 3


def test_offset_and_limit_pull_bound(env: Environment) -> None:
    stream = CountingStream([1, 2, 3, 4, 5, 6])
    template = env.from_string(
        "{% for item in stream limit: 2 offset: 2 %}{{ item }};{% endfor %}"
    )

    assert template.render(stream=stream) == "3;4;"
    assert stream.pulls == 4


def test_last_when_source_is_shorter_than_limit_pulls_one_terminal_record(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2])
    template = env.from_string(
        "{% for item in stream limit: 3 %}{{ item }}:{{ forloop.last }};{% endfor %}"
    )

    assert template.render(stream=stream) == "1:false;2:true;"
    assert stream.pulls == 3


def test_break_does_not_pull_remaining_window(env: Environment) -> None:
    stream = CountingStream([1, 2, 3, 4, 5])
    template = env.from_string(
        "{% for item in stream limit: 3 %}"
        "{% if item == 2 %}{% break %}{% endif %}"
        "{{ item }};"
        "{% endfor %}"
    )

    assert template.render(stream=stream) == "1;"
    assert stream.pulls == 2


def test_continue_progress_uses_consumed_position_after_break(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2, 3, 4, 5])
    template = env.from_string(
        "{% for item in stream limit: 3 %}"
        "{% if item == 2 %}{% break %}{% endif %}"
        "{{ item }};"
        "{% endfor %}"
        "{% for item in stream offset: continue limit: 2 %}{{ item }};{% endfor %}"
    )

    assert template.render(stream=stream) == "1;3;4;"
    assert stream.pulls == 4


def test_unbounded_last_pulls_one_record_beyond_final(env: Environment) -> None:
    stream = CountingStream([1, 2, 3])
    template = env.from_string(
        "{% for item in stream %}{{ item }}:{{ forloop.last }};{% endfor %}"
    )

    assert template.render(stream=stream) == "1:false;2:false;3:true;"
    assert stream.pulls == 4


def test_nested_loops_pull_bounds_independently(env: Environment) -> None:
    children = [
        CountingStream(["a", "b"]),
        CountingStream(["c", "d", "e"]),
    ]
    parents = CountingStream(children, length=2)
    template = env.from_string(
        "{% for child in stream limit: 2 %}"
        "{% for item in child limit: 2 %}{{ item }};{% endfor %}"
        "{% endfor %}"
    )

    assert template.render(stream=parents) == "a;b;c;d;"
    assert parents.pulls == 2
    assert children[0].pulls == 2
    assert children[1].pulls == 2


def test_unknown_outer_loop_can_contain_limited_inner_loop(
    env: Environment,
) -> None:
    children = [
        CountingStream(["a", "b", "c"]),
        CountingStream(["d", "e"]),
    ]
    parents = CountingStream(children)
    template = env.from_string(
        "{% for child in stream %}"
        "{% for item in child limit: 2 %}{{ item }};{% endfor %}"
        "{% endfor %}"
    )

    assert template.render(stream=parents) == "a;b;d;e;"
    assert parents.pulls == 3
    assert children[0].pulls == 2
    assert children[1].pulls == 2


def test_continue_progress_does_not_cross_renders(env: Environment) -> None:
    source = (
        "{% for item in stream limit: 2 %}{{ item }};{% endfor %}"
        "{% for item in stream offset: continue limit: 2 %}{{ item }};{% endfor %}"
    )
    first_stream = CountingStream([1, 2, 3, 4, 5])
    template = env.from_string(source)

    assert template.render(stream=first_stream) == "1;2;3;4;"
    assert first_stream.pulls == 4

    second_stream = CountingStream([1, 2, 3, 4, 5])
    assert template.render(stream=second_stream) == "1;2;3;4;"
    assert second_stream.pulls == 4


def test_length_and_rindex_are_rejected_for_unknown_streams(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2])
    template = env.from_string(
        "{% for item in stream %}{{ forloop.length }}{% endfor %}"
    )

    with pytest.raises(LiquidTypeError, match="forloop.length"):
        template.render(stream=stream)

    stream = CountingStream([1, 2])
    template = env.from_string(
        "{% for item in stream %}{{ forloop.rindex }}{% endfor %}"
    )

    with pytest.raises(LiquidTypeError, match="forloop.rindex"):
        template.render(stream=stream)

    assert stream.pulls == 1


def test_reversed_is_rejected_for_unknown_streams(env: Environment) -> None:
    stream = CountingStream([1, 2])
    template = env.from_string(
        "{% for item in stream reversed %}{{ item }}{% endfor %}"
    )

    with pytest.raises(LiquidTypeError, match="can't reverse"):
        template.render(stream=stream)

    assert stream.pulls == 0


def test_unknown_reversed_rejects_before_consuming_offset(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2])
    template = env.from_string(
        "{% for item in stream offset: 100 reversed %}{{ item }}{% endfor %}"
    )

    with pytest.raises(LiquidTypeError, match="can't reverse"):
        template.render(stream=stream)

    assert stream.pulls == 0


def test_known_length_supports_size_and_filter_reverse_without_len(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2, 3], length=3)

    assert env.from_string("{{ stream | size }}").render(stream=stream) == "3"
    assert stream.pulls == 0

    stream = CountingStream([1, 2, 3], length=3)
    assert env.from_string("{{ stream | reverse | join: ',' }}").render(
        stream=stream
    ) == "3,2,1"
    assert stream.pulls == 4


def test_unbounded_unknown_stream_requires_no_limit_when_limit_is_configured() -> None:
    class LimitedEnvironment(Environment):
        loop_iteration_limit = 10

    stream = CountingStream([1, 2, 3])
    template = LimitedEnvironment().from_string(
        "{% for item in stream %}{{ item }}{% endfor %}"
    )

    with pytest.raises(LoopIterationLimitError):
        template.render(stream=stream)

    assert stream.pulls == 0


def test_known_length_stream_supports_length_and_reversed(env: Environment) -> None:
    stream = CountingStream([1, 2, 3], length=3)
    template = env.from_string(
        "{% for item in stream reversed %}"
        "{{ item }}:{{ forloop.length }}:{{ forloop.rindex }};"
        "{% endfor %}"
    )

    assert template.render(stream=stream) == "3:3:3;2:3:2;1:3:1;"
    assert stream.pulls == 3


def test_reversed_advances_lazy_cursor_for_continue(env: Environment) -> None:
    stream = CountingStream([1, 2, 3], length=3)
    template = env.from_string(
        "{% for item in stream reversed %}{{ item }};{% endfor %}"
        "{% for item in stream offset: continue %}{{ item }};{% endfor %}"
    )

    assert template.render(stream=stream) == "3;2;1;"
    assert stream.pulls == 3


def test_reversed_window_advances_cursor_by_pulled_records(
    env: Environment,
) -> None:
    stream = CountingStream([1, 2, 3, 4], length=4)
    template = env.from_string(
        "{% for item in stream limit: 2 reversed %}{{ item }};{% endfor %}"
        "{% for item in stream offset: continue %}{{ item }};{% endfor %}"
    )

    assert template.render(stream=stream) == "2;1;3;4;"
    assert stream.pulls == 4
