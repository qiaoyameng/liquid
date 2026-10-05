"""Test cases for the extra `component`, `slot` and `yield` tags."""

import asyncio
from typing import Any

import pytest

from liquid import DictLoader
from liquid import Environment
from liquid.exceptions import ContextDepthError
from liquid.exceptions import LiquidSyntaxError
from liquid.extra import ComponentTag
from liquid.extra import SlotTag
from liquid.extra import YieldTag


class MockEnv(Environment):
    keyword_assignment = True


def make_env(partials: dict[str, str] | None = None, **kwargs: Any) -> Environment:
    """Return an environment with the component tags enabled."""
    return MockEnv(
        extra=True,
        loader=DictLoader(partials or {}),
        **kwargs,
    )


def test_render_a_simple_component() -> None:
    """Test that the component decides when slots are rendered."""
    env = make_env(
        {
            "email": (
                "<head>{% yield header %}</head>"
                "<body>{% yield body %}</body>"
            ),
        }
    )
    source = (
        "{% component 'email' %}"
        "{% slot header %}H{% endslot %}"
        "{% slot body %}B{% endslot %}"
        "{% endcomponent %}"
    )
    assert env.render(source) == "<head>H</head><body>B</body>"


def test_slot_body_keeps_caller_scope() -> None:
    """Test that slot bodies resolve variables in the caller's scope."""
    env = make_env(
        {"c": "{{ greeting }}/{% yield s %}"},
    )
    source = (
        "{% assign who = 'caller' %}"
        "{% component 'c', greeting: 'comp' %}"
        "{% slot s %}{{ who }}{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "comp/caller"


def test_component_does_not_see_caller_locals() -> None:
    """Test that component bodies are isolated from the caller's locals."""
    env = make_env({"c": "{{ secret | default: 'hidden' }}"})
    source = (
        "{% assign secret = 'shh' %}{% component 'c' %}{% endcomponent %}"
    )
    assert env.render(source) == "hidden"


def test_component_assignments_do_not_leak() -> None:
    """Test that assigns inside a component do not reach the caller."""
    env = make_env({"c": "{% assign inside = 1 %}"})
    source = (
        "{% component 'c' %}{% endcomponent %}"
        "{{ inside | default: 'none' }}"
    )
    assert env.render(source) == "none"


def test_slot_assignments_do_not_affect_later_yields() -> None:
    """Test that a slot gets a fresh local namespace for each yield."""
    env = make_env({"c": "{% yield s %}|{% yield s %}"})
    source = (
        "{% component 'c' %}"
        "{% slot s %}"
        "{% assign x = x | default: 0 | plus: 1 %}{{ x }}"
        "{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "1|1"

    # Assignments inside the slot do not reach the caller either.
    source2 = (
        "{% component 'c' %}{% slot s %}{% assign x = 5 %}{{ x }}{% endslot %}"
        "{% endcomponent %}{{ x | default: 'none' }}"
    )
    assert env.render(source2) == "5|5none"


def test_same_slot_rendered_twice_uses_caller_scope_each_time() -> None:
    """Test the same slot rendered twice sees the same caller variables."""
    env = make_env({"c": "{% yield s %}|{% yield s %}"})
    source = (
        "{% assign who = 'me' %}{% component 'c' %}"
        "{% slot s %}{{ who }}{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "me|me"


def test_component_keyword_arguments() -> None:
    """Test that limited named arguments are passed to the component."""
    env = make_env({"c": "{{ greeting }}, {{ who | default: 'nobody' }}"})
    source = "{% component 'c', greeting: 'hi', who: 'you' %}{% endcomponent %}"
    assert env.render(source) == "hi, you"


def test_component_arguments_do_not_leak_to_caller_or_slots() -> None:
    """Test that component args stay inside the component."""
    env = make_env({"c": "[{{ greeting }}][{% yield s %}]"})
    source = (
        "{% component 'c', greeting: 'hi' %}"
        "{% slot s %}{{ greeting | default: 'undef' }}{% endslot %}"
        "{% endcomponent %}{{ greeting | default: 'undef' }}"
    )
    assert env.render(source) == "[hi][undef]undef"


def test_yield_parameters_are_scoped_to_the_slot() -> None:
    """Test that a yield can pass a limited set of named parameters."""
    env = make_env(
        {"c": "{% assign x = 'comp' %}{% yield s, item: x, n: 2 %}"},
    )
    source = (
        "{% assign x = 'caller' %}{% component 'c' %}"
        "{% slot s %}{{ item }}-{{ n }}-{{ x }}{% endslot %}"
        "{% endcomponent %}"
    )
    assert env.render(source) == "comp-2-caller"


def test_default_slot() -> None:
    """Test that an unnamed slot is yielded with a bare yield tag."""
    env = make_env({"c": "[{% yield %}][{% yield named %}]"})
    source = (
        "{% component 'c' %}"
        "{% slot %}main{% endslot %}"
        "{% slot named %}side{% endslot %}"
        "{% endcomponent %}"
    )
    assert env.render(source) == "[main][side]"


def test_default_content_when_slot_not_provided() -> None:
    """Test the component-defined default for an unsupplied slot."""
    env = make_env(
        {"c": "{% yield missing %}default{% endyield %}"},
    )
    source = "{% component 'c' %}{% endcomponent %}"
    assert env.render(source) == "default"


def test_default_content_for_unknown_yield_name() -> None:
    """Test that unknown yield names render their default content."""
    env = make_env({"c": "[{% yield nope %}d{% endyield %}]"})
    source = (
        "{% component 'c' %}{% slot other %}x{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "[d]"


def test_unknown_yield_without_default_is_empty() -> None:
    """Test that an inline yield without a slot renders nothing."""
    env = make_env({"c": "a{% yield nope %}b"})
    assert env.render("{% component 'c' %}{% endcomponent %}") == "ab"


def test_provided_slot_overrides_default_content() -> None:
    """Test that caller content takes precedence over default content."""
    env = make_env({"c": "{% yield s %}default{% endyield %}"})
    source = (
        "{% component 'c' %}{% slot s %}override{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "override"


def test_default_content_can_include_other_tags() -> None:
    """Test that default content is parsed as a regular block."""
    env = make_env(
        {
            "c": (
                "{% for i in (1..3) %}{% yield s %}"
                "{% if i == 2 %}two{% else %}n{% endif %}{% endyield %}"
                "{% endfor %}"
            ),
        }
    )
    assert env.render("{% component 'c' %}{% endcomponent %}") == "ntwon"


def test_nested_components_resolve_slots_per_component() -> None:
    """Test nested components with the same slot name."""
    env = make_env(
        {
            "outer": (
                "O[{% component 'inner' %}"
                "{% slot g %}outer-g{% endslot %}"
                "{% endcomponent %}][{% yield g %}of{% endyield %}]"
            ),
            "inner": "I[{% yield g %}ig{% endyield %}]",
        }
    )
    source = (
        "{% component 'outer' %}{% slot g %}root-g{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "O[I[outer-g]][root-g]"


def test_slot_in_a_loop_uses_caller_loop_variables() -> None:
    """Test slot scope when a component is rendered from within a loop."""
    env = make_env({"c": "{{ value }}|{% yield s %}"})
    source = (
        "{% for item in items %}{% component 'c', value: item %}"
        "{% slot s %}loop-{{ item }}-{{ forloop.index0 }}{% endslot %}"
        "{% endcomponent %}{% endfor %}"
    )
    assert env.render(source, items=["a", "b"]) == (
        "a|loop-a-0b|loop-b-1"
    )


def test_duplicate_slot_names_are_a_syntax_error() -> None:
    """Test that repeating a slot name raises at parse time."""
    env = make_env({"c": "{% yield a %}"})
    source = (
        "{% component 'c' %}"
        "{% slot a %}1{% endslot %}{% slot a %}2{% endslot %}"
        "{% endcomponent %}"
    )
    with pytest.raises(LiquidSyntaxError):
        env.from_string(source)


def test_duplicate_default_slot_is_a_syntax_error() -> None:
    """Test that two unnamed slots raise at parse time."""
    env = make_env({"c": "{% yield %}"})
    source = (
        "{% component 'c' %}{% slot %}1{% endslot %}{% slot %}2{% endslot %}"
        "{% endcomponent %}"
    )
    with pytest.raises(LiquidSyntaxError):
        env.from_string(source)


def test_non_slot_content_in_component_body_is_a_syntax_error() -> None:
    """Test that a component body can only contain slots and whitespace."""
    env = make_env({"c": ""})
    with pytest.raises(LiquidSyntaxError):
        env.from_string("{% component 'c' %}hello{% endcomponent %}")


def test_slot_outside_a_component_renders_nothing() -> None:
    """Test that a stray slot tag produces no output."""
    env = make_env({"c": "x"})
    assert env.render("a{% slot s %}hidden{% endslot %}b") == "ab"


def test_recursive_component_respects_context_depth_limit() -> None:
    """Test that recursive components hit the existing depth limit."""

    class LimitedEnv(MockEnv):
        context_depth_limit = 5

    env = LimitedEnv(
        extra=True,
        loader=DictLoader(
            {
                "rec": (
                    "{% component 'rec' %}{% slot %}x{% endslot %}"
                    "{% endcomponent %}"
                ),
            }
        ),
    )
    with pytest.raises(ContextDepthError):
        env.render("{% component 'rec' %}{% endcomponent %}")


def test_autoescape_applies_to_slot_content() -> None:
    """Test that slot output follows the environment autoescape setting."""
    env = make_env({"c": "<{{ x }}>[{% yield s %}]"}, autoescape=True)
    source = (
        "{% component 'c' %}{% slot s %}{{ x }}{% endslot %}{% endcomponent %}"
    )
    assert env.render(source, x="<b>&") == (
        "<&lt;b&gt;&amp;>[&lt;b&gt;&amp;]"
    )


def test_sync_and_async_rendering_match() -> None:
    """Test that asynchronous rendering behaves like synchronous rendering."""
    env = make_env(
        {
            "c": (
                "{% assign compvar = 1 %}"
                "[{% yield h %}][{% yield h %}]"
                "[{% yield b, label: who %}][{% yield missing %}d{% endyield %}]"
            ),
        }
    )
    source = (
        "{% assign who = 'you' %}{% component 'c', who: who %}"
        "{% slot h %}H-{{ who }}{% assign z = 1 %}{{ z }}{% endslot %}"
        "{% slot b %}B-{{ label }}-{{ z | default: 0 }}{% endslot %}"
        "{% endcomponent %}"
        "{{ compvar | default: 'none' }}"
    )

    async def coro() -> str:
        return await env.render_async(source)

    assert asyncio.run(coro()) == env.render(source)


def test_tags_can_be_enabled_without_enabling_all_extra_tags() -> None:
    """Test explicit registration of the three tags."""
    env = Environment(loader=DictLoader({"c": "{% yield %}"}))
    assert "component" not in env.tags
    env.add_tag(ComponentTag)
    env.add_tag(SlotTag)
    env.add_tag(YieldTag)
    result = env.render(
        "{% component 'c' %}{% slot %}hi{% endslot %}{% endcomponent %}"
    )
    assert result == "hi"


def test_standard_include_and_render_are_unchanged() -> None:
    """Test that ordinary include/render tags keep working as before."""
    env = make_env(
        {
            "snippet": "{{ greeting }}",
        }
    )
    assert env.render("{% render 'snippet', greeting: 'r' %}") == "r"
    assert env.render("{% include 'snippet', greeting: 'i' %}") == "i"


def test_quoted_slot_names() -> None:
    """Test that slot names can be quoted strings."""
    env = make_env({"c": "{% yield 'header' %}"})
    source = (
        "{% component 'c' %}{% slot 'header' %}H{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "H"


def test_yield_accepts_keyword_arguments_with_equals() -> None:
    """Test yield parameters with the optional keyword assignment syntax."""
    env = make_env({"c": "{% yield s, n=3 %}"})
    source = (
        "{% component 'c' %}{% slot s %}{{ n }}{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "3"


def test_default_content_inside_a_loop() -> None:
    """Test block yields used repeatedly from inside a loop."""
    env = make_env(
        {
            "c": (
                "{% for i in (1..3) %}{% yield s %}d{{ i }}{% endyield %}"
                "{% endfor %}"
            ),
        }
    )
    assert env.render("{% component 'c' %}{% endcomponent %}") == "d1d2d3"


def test_supplied_slot_repeats_inside_a_loop() -> None:
    """Test caller slots rendered once per loop iteration."""
    env = make_env(
        {"c": "{% for i in (1..3) %}{% yield s, n: i %}{% endfor %}"},
    )
    source = (
        "{% component 'c' %}{% slot s %}[{{ n }}]{% endslot %}{% endcomponent %}"
    )
    assert env.render(source) == "[1][2][3]"


def test_whitespace_between_slots_is_ignored() -> None:
    """Test that slots may be separated by whitespace and newlines."""
    env = make_env({"c": "{% yield a %}{% yield b %}"})
    source = (
        "{% component 'c' %}\n  "
        "{% slot a %}A{% endslot %}\n  "
        "{% slot b %}B{% endslot %}\n"
        "{% endcomponent %}"
    )
    assert env.render(source) == "AB"
