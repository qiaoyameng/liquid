"""Test cases for the opt-in `component`, `slot` and `yield` tags."""

import asyncio
import unittest
from typing import TYPE_CHECKING
from typing import Any

from liquid import DictLoader
from liquid.environment import Environment
from liquid.exceptions import ContextDepthError
from liquid.exceptions import LiquidError

if TYPE_CHECKING:
    from liquid.template import BoundTemplate


class MockEnv(Environment):
    """An environment with all non-standard tags and filters enabled."""


CARD = (
    '<div class="card">'
    "<h1>{% yield header %}</h1>"
    "<p>{% yield body %}default body{% endyield %}</p>"
    "<footer>{% yield footer %}</footer>"
    "<h1>{% yield header %}</h1>"
    "</div>"
)


def _render(
    source: str,
    partials: dict[str, str],
    *,
    autoescape: bool = False,
    **globals: Any,
) -> tuple[str, str]:
    """Render _source_ synchronously and asynchronously."""
    env = MockEnv(extra=True, autoescape=autoescape, loader=DictLoader(partials))
    template = env.from_string(source, globals=globals)

    async def coro() -> str:
        return await template.render_async()

    return template.render(), asyncio.run(coro())


class ComponentTagTestCase(unittest.TestCase):
    """Render `component` tags synchronously and asynchronously."""

    def assert_renders(
        self,
        source: str,
        expect: str,
        partials: dict[str, str] | None = None,
        **globals: Any,
    ) -> None:
        """Assert source renders to expect under both sync and async paths."""
        sync, asy = _render(source, partials or {}, **globals)
        self.assertEqual(sync, expect)
        self.assertEqual(asy, expect)

    def test_named_slots(self) -> None:
        """Test that a component renders slots where it yields them."""
        source = (
            "{% assign x = 'caller-x' %}"
            "{% component 'card' %}"
            "{% slot header %}H-{{ x }}{% endslot %}"
            "{% slot body %}B-{{ x }}{% endslot %}"
            "{% endcomponent %}"
        )
        expect = (
            '<div class="card"><h1>H-caller-x</h1><p>B-caller-x</p>'
            "<footer></footer><h1>H-caller-x</h1></div>"
        )
        self.assert_renders(source, expect, {"card": CARD})

    def test_default_slot_content(self) -> None:
        """Test that yield's default content is used when no slot is given."""
        source = (
            "{% component 'card' %}{% slot header %}H{% endslot %}{% endcomponent %}"
        )
        expect = (
            '<div class="card"><h1>H</h1><p>default body</p>'
            "<footer></footer><h1>H</h1></div>"
        )
        self.assert_renders(source, expect, {"card": CARD})

    def test_unnamed_slot(self) -> None:
        """Test that content outside named slots fills the default yield."""
        self.assert_renders(
            "{% component 'c' %}hello {{ x }}{% endcomponent %}",
            "hello |x",
            {"c": "{% yield %}|{% yield body %}x{% endyield %}"},
        )

    def test_unknown_yield_renders_nothing(self) -> None:
        """Test stable behavior for a yield name without a matching slot."""
        self.assert_renders(
            "{% component 'c' %}{% endcomponent %}",
            "[][fb]",
            {"c": "[{% yield nope %}][{% yield other %}fb{% endyield %}]"},
        )

    def test_duplicate_slot_last_wins(self) -> None:
        """Test that the last definition wins for a repeated slot name."""
        source = (
            "{% component 'c' %}{% slot s %}first{% endslot %}"
            "{% slot s %}second{% endslot %}{% endcomponent %}"
        )
        self.assert_renders(source, "second", {"c": "{% yield s %}"})

    def test_component_assignments_do_not_leak(self) -> None:
        """Test that assignments inside a component stay isolated."""
        self.assert_renders(
            "{% component 'c' a: 42 %}{% endcomponent %}"
            "outside={{ local | default: 'unset' }}",
            "arg=42|local=unset|after=insideoutside=unset",
            {
                "c": (
                    "arg={{ a }}|local={{ local | default: 'unset' }}|"
                    "{% assign local = 'inside' %}after={{ local }}"
                )
            },
        )

    def test_component_cannot_see_caller_locals(self) -> None:
        """Test that only keyword arguments enter the component scope."""
        self.assert_renders(
            "{% assign outer = 'X' %}"
            "{% component 'c' a: outer %}{% endcomponent %}",
            "arg=X|outer=undef",
            {"c": "arg={{ a }}|outer={{ outer | default: 'undef' }}"},
        )

    def test_slot_keeps_caller_scope(self) -> None:
        """Test that slot blocks resolve variables at the call site."""
        self.assert_renders(
            "{% assign g = 'G' %}{% component 'c' g: 'ignored' %}"
            "{% slot s %}{{ g }}{% endslot %}{% endcomponent %}",
            "ignored|G",
            {"c": "{{ g }}|{% yield s %}"},
        )

    def test_slot_assignment_does_not_affect_next_yield(self) -> None:
        """Test that local assignments in a slot don't persist across yields."""
        source = (
            "{% assign g = 'G' %}{% component 'c' g: 'ignored' %}"
            "{% slot s %}{{ g }}{% assign g = 'CHANGED' %}{% endslot %}"
            "{% endcomponent %}after={{ g }}"
        )
        self.assert_renders(
            source,
            "ignored|G|Gafter=G",
            {"c": "{{ g }}|{% yield s %}|{% yield s %}"},
        )

    def test_yield_arguments(self) -> None:
        """Test that yield arguments are evaluated in the component scope."""
        source = (
            "{% assign foo = 'caller-foo' %}{% component 'c' foo: 'comp-foo' %}"
            "{% slot s %}{{ item }}|{{ foo }}{% endslot %}{% endcomponent %}"
        )
        self.assert_renders(
            source,
            "comp-foo|caller-foo",
            {"c": "{% yield s item: foo %}"},
        )

    def test_same_slot_rendered_twice_in_a_loop(self) -> None:
        """Test that a slot in a loop gets fresh params and fresh locals."""
        source = (
            "{% assign seen = 'none' %}"
            "{% component 'row' items: items %}"
            "{% slot cell %}{{ n }}/{% assign seen = n %}{{ seen }}{% endslot %}"
            "{% endcomponent %}after={{ seen }}"
        )
        self.assert_renders(
            source,
            "[1/1][2/2][3/3]after=none",
            {"row": "{% for i in items %}[{% yield cell n: i %}]{% endfor %}"},
            items=[1, 2, 3],
        )

    def test_nested_components_use_lexical_scope(self) -> None:
        """Test variable provenance with nested components."""
        self.assert_renders(
            "{% assign x = 'TOPX' %}{% component 'outer' %}{% endcomponent %}",
            "OUTER(OUTERLOCALINNER[c=innerval]inner-slot:OUTERLOCAL)",
            {
                "outer": (
                    "{% assign x = 'OUTERLOCAL' %}OUTER({{ x }}"
                    "{% component 'inner' c: 'innerval' %}"
                    "{% slot inner_slot %}inner-slot:{{ x }}{% endslot %}"
                    "{% endcomponent %})"
                ),
                "inner": "INNER[c={{ c }}]{% yield inner_slot %}",
            },
        )

    def test_slot_and_quoted_names(self) -> None:
        """Test that bare words and quoted strings name slots identically."""
        self.assert_renders(
            "{% component 'c' %}{% slot 'a' %}A{% endslot %}"
            "{% slot b %}B{% endslot %}{% endcomponent %}",
            "[A][B]",
            {"c": "[{% yield 'a' %}][{% yield b %}]"},
        )

    def test_component_name_from_variable(self) -> None:
        """Test that a variable component name resolves to a template name."""
        self.assert_renders(
            "{% assign which = 'c' %}{% component which %}"
            "{% slot a %}A{% endslot %}{% endcomponent %}",
            "A",
            {"c": "{% yield a %}"},
        )

    def test_default_content_can_contain_tags(self) -> None:
        """Test that yield default content parses arbitrary nested tags."""
        self.assert_renders(
            "{% component 'c' %}{% endcomponent %}",
            "IF|",
            {"c": "{% yield s %}{% if true %}IF{% endif %}{% endyield %}|{% yield s %}"},
        )

    def test_nested_block_form_yields(self) -> None:
        """Test nested yields with and without provided slots."""
        self.assert_renders(
            "{% component 'c' %}{% slot inner %}I{% endslot %}{% endcomponent %}",
            "O-I-O",
            {
                "c": (
                    "{% yield outer %}O-{% yield inner %}INNERDEF{% endyield %}"
                    "-O{% endyield %}"
                )
            },
        )


class ComponentAutoEscapeTestCase(unittest.TestCase):
    """Test that slots follow the same escaping rules as normal fragments."""

    def test_autoescape(self) -> None:
        """Test raw strings are escaped in slots when auto escaping is on."""
        source = (
            "{% component 'c' g: 1 %}"
            "{% slot s %}{{ v }}{% endslot %}{% endcomponent %}"
        )
        sync, asy = _render(
            source,
            {"c": "{{ g }}|{% yield s %}|{% yield s %}"},
            autoescape=True,
            v="<b>",
        )
        self.assertEqual(sync, "1|&lt;b&gt;|&lt;b&gt;")
        self.assertEqual(asy, "1|&lt;b&gt;|&lt;b&gt;")

    def test_no_autoescape(self) -> None:
        """Test that slots are not escaped when auto escaping is disabled."""
        source = "{% component 'c' %}{% slot s %}{{ v }}{% endslot %}{% endcomponent %}"
        sync, asy = _render(source, {"c": "{% yield s %}"}, v="<b>")
        self.assertEqual(sync, "<b>")
        self.assertEqual(asy, "<b>")


class ComponentSyntaxTestCase(unittest.TestCase):
    """Test syntax error handling for the new tags."""

    def setUp(self) -> None:
        self.env = MockEnv(extra=True, loader=DictLoader({"c": "x"}))

    def test_unclosed_component(self) -> None:
        """Test that an unclosed component tag is a syntax error."""
        with self.assertRaises(LiquidError):
            self.env.from_string("{% component 'c' %}")

    def test_unclosed_slot(self) -> None:
        """Test that an unclosed slot tag is a syntax error."""
        with self.assertRaises(LiquidError):
            self.env.from_string(
                "{% component 'c' %}{% slot a %}hi{% endcomponent %}"
            )

    def test_stray_endyield(self) -> None:
        """Test that an unmatched endyield tag is a syntax error."""
        with self.assertRaises(LiquidError):
            self.env.from_string("{% endyield %}")

    def test_slot_outside_component_renders_blank(self) -> None:
        """Test that slot tags on their own capture but never render."""
        template = self.env.from_string("a{% slot x %}hi{% endslot %}b")
        self.assertEqual(template.render(), "ab")

    def test_yield_outside_component_uses_default(self) -> None:
        """Test that a stray yield renders its default content."""
        template = self.env.from_string("a{% yield x %}fb{% endyield %}b")
        self.assertEqual(template.render(), "afbb")


class ComponentRecursionTestCase(unittest.TestCase):
    """Test that recursive components use the existing context depth limit."""

    def test_recursive_component_raises_depth_error(self) -> None:
        """Test that infinite recursion hits the context depth limit."""
        env = MockEnv(
            extra=True,
            loader=DictLoader({"inf": "{% component 'inf' %}{% endcomponent %}"}),
        )
        template = env.from_string("{% component 'inf' %}{% endcomponent %}")
        with self.assertRaises(ContextDepthError):
            template.render()

    def test_finite_component_chain(self) -> None:
        """Test nested components pass arguments down the chain."""
        env = MockEnv(
            extra=True,
            loader=DictLoader(
                {
                    "r0": "base[{% component 'r1' a: 1 %}{% endcomponent %}]",
                    "r1": "r1({{ a }})[{% component 'r2' b: a %}{% endcomponent %}]",
                    "r2": "r2({{ b }})",
                }
            ),
        )
        template = env.from_string("{% component 'r0' %}{% endcomponent %}")
        self.assertEqual(template.render(), "base[r1(1)[r2(1)]]")
