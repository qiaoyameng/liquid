"""Tests for the lazy iterable protocol consumed by the `for` tag.

A lazy iterable models a backend record stream that can only be iterated
once and has no known length. These tests count records pulled from such a
stream, verifying that rendering pulls no more records than an
explainable bound.
"""

import asyncio
from typing import Any
from typing import Iterator

import pytest

from liquid import Environment
from liquid import LazyIterable
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import LoopIterationLimitError


class RecordStream(LazyIterable):
    """A counting, single-pass stream of records."""

    def __init__(self, records: list[Any], *, hint: int | None = "__unknown__"):
        self.records = records
        self.pulls = 0
        self._hint = None if hint == "__unknown__" else hint

    def __iter__(self) -> Iterator[Any]:
        for record in self.records:
            self.pulls += 1
            yield record

    def length_hint(self) -> int | None:
        return self._hint


def render(source: str, stream: RecordStream, **kwargs: Any) -> tuple[str, int]:
    env = Environment()
    result = env.from_string(source).render(rows=stream, **kwargs)
    return result, stream.pulls


def test_empty_stream_renders_else() -> None:
    """An empty stream renders the for-else block with zero pulls."""
    result, pulls = render(
        "{% for r in rows %}{{ r }}{% else %}EMPTY{% endfor %}",
        RecordStream([]),
    )
    assert result == "EMPTY"
    assert pulls == 0


def test_empty_stream_without_else_renders_nothing() -> None:
    result, pulls = render("{% for r in rows %}{{ r }}{% endfor %}", RecordStream([]))
    assert result == ""
    assert pulls == 0


def test_limit_zero_pulls_nothing() -> None:
    """A limit of zero produces no output and pulls no records."""
    result, pulls = render(
        "{% for r in rows limit:0 %}{{ r }}{% else %}ZERO{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "ZERO"
    assert pulls == 0


def test_full_stream_last_uses_single_lookahead() -> None:
    """Rendering every record pulls each record once plus one probe."""
    result, pulls = render(
        "{% for r in rows %}{{ r }}{% if forloop.last %}!{% endif %}|{% endfor %}",
        RecordStream(list(range(5))),
    )
    assert result == "0|1|2|3|4!|"
    assert pulls == 5


def test_no_last_no_extra_pull() -> None:
    """Without referencing `last`, the stream is pulled exactly once each."""
    result, pulls = render(
        "{% for r in rows %}{{ r }},{% endfor %}", RecordStream(list(range(5)))
    )
    assert result == "0,1,2,3,4,"
    assert pulls == 5


def test_limit_bounds_pulls() -> None:
    """A finite limit needs at most limit records plus a lookahead."""
    result, pulls = render(
        "{% for r in rows limit:3 %}"
        "{{ r }}{% if forloop.last %}L{% endif %},{% endfor %}",
        RecordStream(list(range(100))),
    )
    assert result == "0,1,2L,"
    assert pulls == 3


def test_last_false_before_end_with_limit() -> None:
    result, pulls = render(
        "{% for r in rows limit:3 %}"
        "{% if forloop.last %}L{% else %}{{ r }},{% endif %}{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "0,1,L"
    assert pulls == 3


def test_offset_and_limit_pull_bound() -> None:
    """The pull bound for offset:O limit:L is O + L (+1 lookahead)."""
    result, pulls = render(
        "{% for r in rows offset:5 limit:3 %}"
        "{{ r }}{% if forloop.last %}L{% endif %},{% endfor %}",
        RecordStream(list(range(20))),
    )
    assert result == "5,6,7L,"
    # Five skipped plus three rendered; `last` is known from the limit.
    assert pulls == 8


def test_first_index_with_offset() -> None:
    result, _ = render(
        "{% for r in rows offset:2 limit:2 %}"
        "{% if forloop.first %}F{% endif %}"
        "{{ forloop.index0 }}:{{ r }};{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "F0:2;1:3;"


def test_early_break_stops_pulls() -> None:
    """Breaking never pulls records beyond the one being rendered."""
    result, pulls = render(
        "{% for r in rows %}{{ r }}"
        "{% if r == 2 %}{% break %}{% endif %}{% endfor %}",
        RecordStream(list(range(100))),
    )
    assert result == "012"
    assert pulls == 3


def test_continue_skips_output_not_pulls() -> None:
    result, pulls = render(
        "{% for r in rows %}{% if r == 1 %}{% continue %}{% endif %}"
        "{{ r }},{% endfor %}",
        RecordStream(list(range(4))),
    )
    assert result == "0,2,3,"
    assert pulls == 4


def test_nested_lazy_loops() -> None:
    """Nested lazy streams report first/last correctly."""

    def make() -> RecordStream:
        return RecordStream(
            [
                {"name": "a", "items": RecordStream([10, 11])},
                {"name": "b", "items": RecordStream([20, 21, 22])},
            ]
        )

    outer = make()
    result = (
        Environment()
        .from_string(
            "{% for p in rows %}{{ p.name }}["
            "{% for x in p.items %}{{ x }}"
            "{% if forloop.last %}#{% endif %},{% endfor %}];"
            "{% endfor %}"
        )
        .render(rows=outer)
    )
    assert result == "a[10,11#,];b[20,21,22#,];"
    assert outer.pulls == 2


def test_eager_inner_inside_lazy_outer() -> None:
    """A normal list inside a lazy outer loop keeps eager forloop fields."""
    result, pulls = render(
        "{% for p in rows %}[{% for x in p.items %}"
        "{{ forloop.length }}:{{ x }};{% endfor %}]"
        "{% endfor %}",
        RecordStream([{"items": [1, 2]}, {"items": [3, 4, 5]}]),
    )
    assert result == "[2:1;2:2;][3:3;3:4;3:5;]"
    assert pulls == 2


def test_lazy_inner_inside_eager_outer() -> None:
    env = Environment()
    template = env.from_string(
        "{% for p in rows %}[{% for x in p.items %}"
        "{{ x }}{% if forloop.last %}#{% endif %};{% endfor %}]"
        "{% endfor %}"
    )
    result = template.render(
        rows=[{"items": RecordStream([1, 2])}, {"items": RecordStream([3])}]
    )
    assert result == "[1;2#;][3#;]"


def test_parentloop_points_at_outer() -> None:
    result, _ = render(
        "{% for p in rows %}[{% for x in p.items %}"
        "{{ forloop.parentloop.index }}-{{ x }};"
        "{% endfor %}]{% endfor %}",
        RecordStream([{"items": [1, 2]}, {"items": [3]}]),
    )
    assert result == "[1-1;1-2;][2-3;]"


def test_length_refused_on_unknown_stream() -> None:
    with pytest.raises(LiquidTypeError):
        render(
            "{% for r in rows %}{{ forloop.length }}{% endfor %}",
            RecordStream(list(range(3))),
        )


def test_rindex_refused_on_unknown_stream() -> None:
    with pytest.raises(LiquidTypeError):
        render(
            "{% for r in rows %}{{ forloop.rindex }}{% endfor %}",
            RecordStream(list(range(3))),
        )


def test_rindex0_refused_on_unknown_stream() -> None:
    with pytest.raises(LiquidTypeError):
        render(
            "{% for r in rows %}{{ forloop.rindex0 }}{% endfor %}",
            RecordStream(list(range(3))),
        )


def test_known_hint_supports_length_and_rindex() -> None:
    result, pulls = render(
        "{% for r in rows %}{{ forloop.rindex }}/{{ r }};"
        "{% if forloop.last %}L{% endif %}{% endfor %}",
        RecordStream(list(range(3)), hint=3),
    )
    assert result == "3/0;2/1;1/2;L"
    assert pulls == 3


def test_unbounded_reversed_refused() -> None:
    with pytest.raises(LiquidTypeError):
        render(
            "{% for r in rows reversed %}{{ r }}{% endfor %}",
            RecordStream(list(range(5))),
        )


def test_bounded_reversed_only_buffers_window() -> None:
    result, pulls = render(
        "{% for r in rows offset:2 limit:3 reversed %}{{ r }},{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "4,3,2,"
    assert pulls == 5


def test_source_supports_reversed() -> None:
    class ReversibleStream(RecordStream):
        def reversed(self) -> Iterator[Any]:
            for record in reversed(self.records):
                yield record

    stream = ReversibleStream(list(range(5)))
    result = (
        Environment()
        .from_string("{% for r in rows reversed %}{{ r }},{% endfor %}")
        .render(rows=stream)
    )
    assert result == "4,3,2,1,0,"


def test_offset_continue_within_one_render() -> None:
    """Two paginating loops in one render continue from the first's cursor."""
    result, pulls = render(
        "{% for r in rows limit:3 %}{{ r }};{% endfor %}"
        "{% for r in rows offset:continue limit:3 %}{{ r }};{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "0;1;2;3;4;5;"
    assert pulls == 9


def test_offset_continue_after_break() -> None:
    result, pulls = render(
        "{% for r in rows %}{{ r }}"
        "{% if r == 1 %}{% break %}{% endif %}{% endfor %}|"
        "{% for r in rows offset:continue %}{{ r }}{% endfor %}",
        RecordStream(list(range(5))),
    )
    assert result == "01|234"
    assert pulls == 7


def test_numeric_offset_then_continue_resumes_after_window() -> None:
    """A numeric offset followed by `continue` resumes at the absolute index."""
    result, pulls = render(
        "{% for r in rows offset:5 limit:2 %}{{ r }};{% endfor %}"
        "{% for r in rows offset:continue %}{{ r }},{% endfor %}",
        RecordStream(list(range(10))),
    )
    assert result == "5;6;7,8,9,"
    # The second loop skips seven records itself (factory semantics), so
    # skipped records are pulled again: 5+2 then 7+3, plus a lookahead.
    assert pulls == 17


def test_second_render_does_not_inherit_cursor() -> None:
    """A new render starts fresh even when reusing the same template."""
    env = Environment()
    template = env.from_string(
        "{% for r in rows limit:3 %}{{ r }};{% endfor %}"
    )

    first = template.render(rows=RecordStream(list(range(10))))
    second = template.render(rows=RecordStream(list(range(10))))

    assert first == "0;1;2;"
    assert second == "0;1;2;"


def test_offset_continue_does_not_leak_across_renders() -> None:
    env = Environment()
    template = env.from_string(
        "{% for r in rows offset:continue limit:2 %}{{ r }};{% endfor %}"
    )

    first_stream = RecordStream(list(range(10)))
    template.render(rows=first_stream)

    second_stream = RecordStream(list(range(10)))
    result = template.render(rows=second_stream)

    # Without a prior loop in this render, the cursor starts at zero.
    assert result == "0;1;"
    assert second_stream.pulls == 2


def test_infinite_stream_respects_iteration_limit() -> None:
    """An unknown-length stream can not defeat the loop iteration limit."""

    class InfiniteStream(LazyIterable):
        def __init__(self) -> None:
            self.pulls = 0

        def __iter__(self) -> Iterator[Any]:
            n = 0
            while True:
                self.pulls += 1
                yield n
                n += 1

    class LimitedEnv(Environment):
        loop_iteration_limit = 50

    env = LimitedEnv()
    stream = InfiniteStream()
    with pytest.raises(LoopIterationLimitError):
        env.from_string("{% for r in rows %}x{% endfor %}").render(rows=stream)

    # One extra lookahead probe at most beyond the bounded iterations.
    assert stream.pulls <= 52


def test_infinite_stream_explicit_limit_renders_only_window() -> None:
    class InfiniteStream(LazyIterable):
        def __init__(self) -> None:
            self.pulls = 0

        def __iter__(self) -> Iterator[Any]:
            n = 0
            while True:
                self.pulls += 1
                yield n
                n += 1

    env = Environment()
    stream = InfiniteStream()
    result = (
        env.from_string("{% for r in rows limit:4 %}{{ r }},{% endfor %}")
        .render(rows=stream)
    )
    assert result == "0,1,2,3,"
    assert stream.pulls == 4


def test_tablerow_rejects_unknown_length() -> None:
    with pytest.raises(LiquidTypeError):
        render(
            "{% tablerow r in rows %}{{ r }}{% endtablerow %}",
            RecordStream(list(range(3))),
        )


def test_async_render_of_lazy_stream() -> None:
    env = Environment()
    template = env.from_string(
        "{% for r in rows limit:2 %}{{ r }}{% if forloop.last %}L{% endif %};"
        "{% endfor %}"
    )
    stream = RecordStream(list(range(5)))

    result = asyncio.run(template.render_async(rows=stream))
    assert result == "0;1L;"
    assert stream.pulls == 2


def test_async_second_render_starts_fresh() -> None:
    env = Environment()
    template = env.from_string(
        "{% for r in rows offset:continue limit:2 %}{{ r }};{% endfor %}"
    )
    asyncio.run(template.render_async(rows=RecordStream(list(range(10)))))
    stream = RecordStream(list(range(10)))
    result = asyncio.run(template.render_async(rows=stream))
    assert result == "0;1;"
    assert stream.pulls == 2
