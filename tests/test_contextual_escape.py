"""Tests for the opt-in, context aware HTML escaping mode."""

from __future__ import annotations

import pytest

from liquid import DEFAULT_ALLOWED_URL_SCHEMES
from liquid import DictLoader
from liquid import Environment
from liquid import Markup
from liquid import Mode
from liquid import SafeFragment
from liquid.safehtml import ContextualEscapeError


def make_env(**kwargs: object) -> Environment:
    kwargs.setdefault("contextual_escape", True)
    return Environment(**kwargs)  # type: ignore[arg-type]


# -- default mode is unchanged ----------------------------------------------


def test_default_mode_does_not_escape_or_validate() -> None:
    env = Environment()
    out = env.from_string('<a href="{{ u }}">{{ x }}</a>').render(
        u="javascript:alert(1)", x="<script>"
    )
    assert out == '<a href="javascript:alert(1)"><script></a>'


def test_contextual_escape_is_opt_in() -> None:
    env = Environment()
    assert env.contextual_escape is False
    env2 = Environment(contextual_escape=True)
    assert env2.contextual_escape is True
    assert env2.autoescape is True


# -- text context -----------------------------------------------------------


def test_escape_text_context() -> None:
    out = make_env().render("<p>{{ x }}</p>", x='<script>alert("x")</script>')
    assert out == (
        "<p>&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;</p>"
    )


def test_escape_text_inside_branch() -> None:
    out = make_env().render(
        "{% if true %}<b>{{ x }}</b>{% endif %}", x="<i>"
    )
    assert out == "<b>&lt;i&gt;</b>"


# -- quoted attribute context ----------------------------------------------


def test_escape_quoted_attribute_double() -> None:
    out = make_env().render(
        '<a title="{{ x }}">y</a>', x='" onclick="alert(1)'
    )
    assert out == '<a title="&#34; onclick=&#34;alert(1)">y</a>'


def test_escape_quoted_attribute_single() -> None:
    out = make_env().render(
        "<a title='{{ x }}'>y</a>", x="' onmouseover='alert(1)"
    )
    assert out == "<a title='&#39; onmouseover=&#39;alert(1)'>y</a>"


def test_attribute_closing_attempt_is_neutralized() -> None:
    payload = '"><script>alert(1)</script>'
    out = make_env().render('<a href="/x" title="{{ x }}">', x=payload)
    assert "<script>" not in out
    assert "&#34;&gt;" in out


# -- URL attribute context --------------------------------------------------


def test_safe_urls_pass() -> None:
    env = make_env()
    for url in (
        "https://example.com/p",
        "http://example.com",
        "/relative/path",
        "#fragment",
        "?query=1",
        "mailto:a@example.com",
        "tel:+15551234",
        "//example.com/protocol-relative",
        "",
    ):
        assert env.render('<a href="{{ u }}">x</a>', u=url) == (
            f'<a href="{url}">x</a>'
        )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)",
        "\tjavascript:alert(1)",
        "\njavascript:alert(1)",
        "java\x00script:alert(1)",
        "java\x09script:alert(1)",
        "\xadjavascript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_dangerous_dynamic_urls_are_rejected(url: str) -> None:
    env = make_env()
    template = env.from_string('<a href="{{ u }}">x</a>')
    with pytest.raises(ContextualEscapeError):
        template.render(u=url)


def test_dynamic_url_is_entity_escaped_in_output() -> None:
    # Entity references in dynamic values must not be decoded by the
    # validator; only literal template text is entity decoded.
    out = make_env().render(
        '<a href="{{ u }}">x</a>', u="&#106;avascript:bad"
    )
    assert out == '<a href="&amp;#106;avascript:bad">x</a>'


@pytest.mark.parametrize(
    "literal",
    [
        '<a href="&#106;avascript:alert(1)">x</a>',
        '<a href="&#x6a;avascript:alert(1)">x</a>',
        '<a href="&#9;javascript:alert(1)">x</a>',
        '<a href="  javascript:alert(1)">x</a>',
        '<a href="java&#x00;script:alert(1)">x</a>',
        '<a href="jav&#x0A;ascript:alert(1)">x</a>',
        '<a href="vbscript:msgbox(1)">x</a>',
        '<a href="data:text/html;base64,PHNjcmlwdD4=">x</a>',
    ],
)
def test_dangerous_literal_urls_are_rejected(literal: str) -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(literal)


def test_mixed_literal_dynamic_scheme_rejected() -> None:
    template = make_env().from_string('<a href="java{{ u }}">x</a>')
    with pytest.raises(ContextualEscapeError):
        template.render(u="script:alert(1)")


def test_allowed_schemes_are_configurable() -> None:
    env = make_env(allowed_url_schemes=frozenset({"custom"}))
    template = env.from_string('<a href="{{ u }}">x</a>')
    assert template.render(u="custom:thing") == '<a href="custom:thing">x</a>'
    with pytest.raises(ContextualEscapeError):
        template.render(u="https://example.com")


def test_default_allowed_schemes_constant() -> None:
    assert "https" in DEFAULT_ALLOWED_URL_SCHEMES
    assert "javascript" not in DEFAULT_ALLOWED_URL_SCHEMES


# -- unsupported positions are rejected ------------------------------------


def test_dynamic_tag_name_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string("<{{ tag }}>x</{{ tag }}>")


def test_dynamic_attribute_name_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string('<div {{ attr }}="x">y</div>')


def test_unquoted_attribute_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string('<a href={{ x }}>y</a>')


@pytest.mark.parametrize(
    "source",
    [
        "<script>var x = {{ y }};</script>",
        "<style>.a { color: {{ c }}; }</style>",
    ],
)
def test_raw_text_elements_are_rejected(source: str) -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(source)


def test_unclosed_attribute_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string('<a title="{{ x }}')


def test_unclosed_tag_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string('<a title="x"{{ y }}')


# -- branches ---------------------------------------------------------------


def test_consistent_branches_are_accepted() -> None:
    env = make_env()
    out = env.from_string(
        '{% if c %}<b>{{ x }}</b>{% else %}<i>{{ x }}</i>{% endif %}'
    ).render(c=True, x="&")
    assert out == "<b>&amp;</b>"


def test_branches_in_different_contexts_are_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '{% if c %}<a title="{% else %}<b>{% endif %}x'
        )


def test_branch_without_else_that_enters_tag_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string('{% if c %}<a title="{% endif %}{{ x }}')


def test_loop_body_is_scanned() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string("{% for n in items %}<script>{{ n }}</script>{% endfor %}")


def test_balanced_loop_renders() -> None:
    out = make_env().render(
        "{% for n in items %}<li>{{ n }}</li>{% endfor %}",
        items=["<a>", "b"],
    )
    assert out == "<li>&lt;a&gt;</li><li>b</li>"


# -- capture ----------------------------------------------------------------


def test_capture_is_re_escaped_in_attribute_context() -> None:
    out = make_env().render(
        '{% capture g %}hello "world"{% endcapture %}'
        '<a title="{{ g }}">x</a>'
    )
    assert out == '<a title="hello &#34;world&#34;">x</a>'


def test_capture_is_raw_in_text_context() -> None:
    out = make_env().render(
        '{% capture g %}<b>{{ x }}</b>{% endcapture %}<p>{{ g }}</p>',
        x="<i>",
    )
    assert out == "<p><b>&lt;i&gt;</b></p>"


def test_unbalanced_capture_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '{% capture g %}<a title="{{ x }}{% endcapture %}{{ g }}'
        )


# -- include / render -------------------------------------------------------


def test_included_balanced_fragment_renders() -> None:
    env = make_env(loader=DictLoader({"card": '<li>{{ item }}</li>'}))
    out = env.from_string('<ul>{% include "card" %}</ul>').render(item="<b>")
    assert out == "<ul><li>&lt;b&gt;</li></ul>"


def test_include_in_attribute_context_is_rejected() -> None:
    env = make_env(loader=DictLoader({"card": "<b>x</b>"}))
    with pytest.raises(ContextualEscapeError):
        env.from_string('<a title="{% include "card" %}">y</a>')


def test_render_in_attribute_context_is_rejected() -> None:
    env = make_env(loader=DictLoader({"card": "<b>x</b>"}))
    with pytest.raises(ContextualEscapeError):
        env.from_string('<a title="{% render "card" %}">y</a>')


def test_unbalanced_included_template_is_rejected() -> None:
    env = make_env(loader=DictLoader({"bad": '<a title="{{ x }}"'}))
    with pytest.raises(ContextualEscapeError):
        env.from_string('{% include "bad" %}')


def test_dynamic_include_name_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string("{% include name %}")


def test_included_script_is_rejected() -> None:
    env = make_env(loader=DictLoader({"js": "<script>{{ x }}</script>"}))
    with pytest.raises(ContextualEscapeError):
        env.from_string('{% include "js" %}')


# -- marked safe values have no universal privilege -------------------------


def test_markup_is_raw_in_text_but_escaped_in_attribute() -> None:
    env = make_env()
    assert env.render("{{ x }}", x=Markup("<b>ok</b>")) == "<b>ok</b>"
    out = env.render(
        '<a title="{{ x }}">y</a>', x=Markup('" onmouseover="x')
    )
    assert out == '<a title="&#34; onmouseover=&#34;x">y</a>'


def test_markup_url_is_scheme_checked() -> None:
    env = make_env()
    template = env.from_string('<a href="{{ x }}">y</a>')
    with pytest.raises(ContextualEscapeError):
        template.render(x=Markup("javascript:alert(1)"))


def test_safe_fragment_is_raw_only_in_text() -> None:
    env = make_env()
    assert env.render("{{ x }}", x=SafeFragment("<b>ok</b>")) == "<b>ok</b>"
    out = env.render(
        '<a title="{{ x }}">y</a>', x=SafeFragment('" x="y')
    )
    assert "&#34;" in out


# -- error policy -----------------------------------------------------------


def test_contextual_errors_are_raised_even_in_lax_mode() -> None:
    env = make_env(tolerance=Mode.LAX)
    with pytest.raises(ContextualEscapeError):
        env.from_string("<script>{{ x }}</script>")


def test_contextual_errors_propagate_at_render() -> None:
    env = make_env()
    template = env.from_string('<a href="{{ u }}">x</a>')
    # Render-time failures propagate; they are not swallowed.
    with pytest.raises(ContextualEscapeError):
        template.render(u="javascript:alert(1)")


# -- attribute/URL context across branches ---------------------------------


def test_branch_within_attribute_uses_attribute_policy() -> None:
    env = make_env()
    template = env.from_string(
        '<a title="{% if c %}{{ a }}{% else %}{{ b }}{% endif %}">x</a>'
    )
    assert template.render(c=True, a='"x', b="y") == '<a title="&#34;x">x</a>'
    assert template.render(c=False, a='"x', b="y") == '<a title="y">x</a>'


def test_branch_that_closes_attribute_is_rejected() -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '<a title="{% if c %}x"{% else %}y{% endif %}>'
        )


def test_url_inside_branch_is_validated_at_runtime() -> None:
    env = make_env()
    template = env.from_string(
        '<a href="{% if c %}{{ u }}{% else %}/default{% endif %}">x</a>'
    )
    assert template.render(c=False, u="javascript:bad") == (
        '<a href="/default">x</a>'
    )
    with pytest.raises(ContextualEscapeError):
        template.render(c=True, u="javascript:bad")


def test_split_scheme_across_outputs_is_rejected() -> None:
    template = make_env().from_string('<a href="{{ a }}{{ b }}">x</a>')
    with pytest.raises(ContextualEscapeError):
        template.render(a="java", b="script:alert(1)")


def test_literal_then_dynamic_scheme_is_rejected() -> None:
    template = make_env().from_string('<a href="java{{ u }}">x</a>')
    with pytest.raises(ContextualEscapeError):
        template.render(u="script:alert(1)")


# -- blank suppression ------------------------------------------------------


def test_blank_suppressed_block_does_not_advance_state() -> None:
    env = make_env()
    out = env.from_string(
        '{% if false %}\n  {{ x }}\n{% endif %}<b>{{ y }}</b>'
    ).render(x="<script>", y="&")
    assert out == "<b>&amp;</b>"


# -- echo and other dynamic tags -------------------------------------------


def test_echo_tag_is_context_escaped() -> None:
    env = make_env()
    assert env.from_string("{% echo x %}").render(x="<b>") == "&lt;b&gt;"
    assert env.from_string('<a title="{% echo x %}">').render(x='"') == (
        '<a title="&#34;">'
    )


def test_increment_is_context_escaped_in_attribute() -> None:
    env = make_env()
    # increment emits digits which are harmless, but still goes through policy.
    out = env.from_string(
        '<a title="{% increment i %}">x</a>'
    ).render()
    assert out == '<a title="0">x</a>'


def test_cycle_is_context_escaped() -> None:
    env = make_env()
    out = env.from_string(
        '<a title="{% cycle x, y %}">x</a>'
    ).render(x='"', y="b")
    assert out == '<a title="&#34;">x</a>'


# -- template factory -------------------------------------------------------


def test_template_factory_supports_contextual_escape() -> None:
    from liquid import Template

    template = Template('<a href="{{ u }}">{{ x }}</a>', contextual_escape=True)
    assert template.render(u="/p", x="<b>") == '<a href="/p">&lt;b&gt;</a>'
    with pytest.raises(ContextualEscapeError):
        template.render(u="javascript:alert(1)", x="")


def test_for_else_branches_must_converge() -> None:
    env = make_env()
    template = env.from_string(
        "{% for n in items %}<li>{{ n }}</li>{% else %}<p>empty</p>{% endfor %}"
    )
    assert template.render(items=[]) == "<p>empty</p>"
    assert template.render(items=["x"]) == "<li>x</li>"

    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '{% for n in items %}{{ n }}{% else %}<a title="x{% endfor %}'
        )

    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '{% for n in items %}<li id="{{ n }}{% else %}<p>e</p>{% endfor %}'
        )


def test_ifchanged_block_must_be_balanced() -> None:
    env = make_env()
    template = env.from_string(
        "{% for n in items %}{% ifchanged %}{{ n }}{% endifchanged %}{% endfor %}"
    )
    assert template.render(items=[1, 1, 2]) == "12"

    with pytest.raises(ContextualEscapeError):
        make_env().from_string(
            '{% ifchanged %}<a title="x{% endifchanged %}tail'
        )


# -- RCDATA elements (title, textarea) -------------------------------------


def test_title_and_textarea_escape_dynamic_values_as_text() -> None:
    env = make_env()
    assert env.render("<title>{{ x }}</title>", x="<b>hi</b>") == (
        "<title>&lt;b&gt;hi&lt;/b&gt;</title>"
    )
    assert env.render("<textarea>{{ x }}</textarea>", x="a&b") == (
        "<textarea>a&amp;b</textarea>"
    )


def test_closing_rcdata_element_in_dynamic_value_is_escaped() -> None:
    out = make_env().render(
        "<textarea>{{ x }}</textarea>",
        x="</textarea><script>alert(1)</script>",
    )
    assert "<script>" not in out
    assert "&lt;/textarea&gt;" in out


@pytest.mark.parametrize("element", ["script", "style", "iframe", "xmp"])
def test_raw_text_elements_reject_dynamic_values(element: str) -> None:
    with pytest.raises(ContextualEscapeError):
        make_env().from_string(f"<{element}>{{{{ x }}}}</{element}>")


def test_repeated_parses_use_fresh_validator() -> None:
    # Regression: a previous valid parse must not cause a later invalid one
    # to be accepted.
    env = make_env()
    env.from_string("<title>hi</title>")
    with pytest.raises(ContextualEscapeError):
        env.from_string("<title>a</title")
    with pytest.raises(ContextualEscapeError):
        env.from_string('<a title="{{ x }}')


def test_recursive_include_is_rejected() -> None:
    env = make_env(
        loader=DictLoader(
            {"a": '{% include "b" %}', "b": '{% include "a" %}'}
        )
    )
    with pytest.raises(ContextualEscapeError):
        env.from_string('{% include "a" %}')
