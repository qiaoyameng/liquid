"""Contextual HTML autoescape test cases."""

import asyncio

import pytest

from liquid import DictLoader
from liquid import Environment
from liquid import Markup
from liquid.exceptions import HTMLContextError


@pytest.fixture()
def env() -> Environment:
    return Environment(contextual_autoescape=True)


def test_contextual_autoescape_is_disabled_by_default() -> None:
    env = Environment()
    assert not env.contextual_autoescape
    assert not env.autoescape
    assert env.from_string("{{ x }}").render(x="<b>") == "<b>"


def test_contextual_autoescape_implies_autoescape(env: Environment) -> None:
    assert env.autoescape


def test_text_context_is_escaped(env: Environment) -> None:
    template = env.from_string("<p>{{ x }}</p>")
    assert template.render(x='<script>alert("XSS")</script>') == (
        "<p>&lt;script&gt;alert(&#34;XSS&#34;)&lt;/script&gt;</p>"
    )


def test_safe_string_is_privileged_in_text_context(env: Environment) -> None:
    assert env.from_string("<p>{{ x | safe }}</p>").render(x="<b>") == "<p><b></p>"
    assert env.from_string("<p>{{ x }}</p>").render(x=Markup("<b>")) == "<p><b></p>"


def test_double_quoted_attribute_context(env: Environment) -> None:
    template = env.from_string('<div class="{{ x }}">')
    # Can not close the attribute or the tag.
    assert template.render(x='foo"><script>alert(1)</script>') == (
        '<div class="foo&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;">'
    )


def test_single_quoted_attribute_context(env: Environment) -> None:
    template = env.from_string("<div class='{{ x }}'>")
    assert template.render(x="foo' onmouseover='alert(1)") == (
        "<div class='foo&#39; onmouseover=&#39;alert(1)'>"
    )


def test_attribute_context_escapes_ampersand(env: Environment) -> None:
    template = env.from_string('<div data-value="{{ x }}">')
    assert template.render(x="a&b<c>") == '<div data-value="a&amp;b&lt;c&gt;">'


def test_safe_string_is_not_privileged_in_attribute_context(
    env: Environment,
) -> None:
    # The `safe` filter does not exempt values from attribute escaping.
    assert env.from_string('<div title="{{ x | safe }}">').render(x="<b>") == (
        '<div title="&lt;b&gt;">'
    )
    # Neither does the `Markup` safe string marker.
    assert env.from_string('<div title="{{ x }}">').render(x=Markup("<b>")) == (
        '<div title="&lt;b&gt;">'
    )


def test_safe_string_is_not_privileged_in_url_context(env: Environment) -> None:
    template = env.from_string('<a href="{{ x | safe }}">')
    with pytest.raises(HTMLContextError):
        template.render(x="javascript:alert(1)")
    with pytest.raises(HTMLContextError):
        env.from_string('<a href="{{ x }}">').render(x=Markup("javascript:alert(1)"))


def test_url_context_allows_safe_protocols(env: Environment) -> None:
    template = env.from_string('<a href="{{ url }}">x</a>')
    assert template.render(url="https://example.com/?a=1&b=2") == (
        '<a href="https://example.com/?a=1&amp;b=2">x</a>'
    )
    assert template.render(url="http://example.com") == (
        '<a href="http://example.com">x</a>'
    )
    assert template.render(url="mailto:you@example.com") == (
        '<a href="mailto:you@example.com">x</a>'
    )
    assert template.render(url="/relative/path") == '<a href="/relative/path">x</a>'
    assert template.render(url="#fragment") == '<a href="#fragment">x</a>'
    assert template.render(url="?q=1") == '<a href="?q=1">x</a>'


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "  javascript:alert(1)",
        "java\tscript:alert(1)",
        "java\nscript:alert(1)",
        "&#106;avascript:alert(1)",
        "java&#115;cript:alert(1)",
        "&#x6a;avascript:alert(1)",
        "vbscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
    ],
)
def test_url_context_rejects_unsafe_protocols(env: Environment, url: str) -> None:
    template = env.from_string('<a href="{{ url }}">x</a>')
    with pytest.raises(HTMLContextError):
        template.render(url=url)


def test_url_attributes(env: Environment) -> None:
    for tag in (
        '<img src="{{ u }}">',
        '<form action="{{ u }}">',
        '<button formaction="{{ u }}">',
        '<video poster="{{ u }}">',
    ):
        with pytest.raises(HTMLContextError):
            env.from_string(tag).render(u="javascript:alert(1)")


def test_non_url_attribute_does_not_check_protocol(env: Environment) -> None:
    # `title` is not a URL attribute, so no protocol validation happens. The
    # value is still attribute-escaped and can not break out of the attribute.
    template = env.from_string('<div title="{{ u }}">')
    assert template.render(u="javascript:alert(1)") == (
        '<div title="javascript:alert(1)">'
    )


@pytest.mark.parametrize(
    "source",
    [
        "<{{ tag }}>",  # dynamic tag name
        "<div class={{ x }}>",  # unquoted attribute value
        "<div {{ name }}='x'>",  # dynamic attribute name
        '<div class="a" {{ x }}>',  # between attributes
        "<div class={{ x }}>",  # unquoted attribute value
        "<script>{{ x }}</script>",
        "<style>{{ x }}</style>",
        "<title>{{ x }}</title>",
        "<textarea>{{ x }}</textarea>",
        "<!-- {{ x }} -->",
        '<div class="unclosed {{ x }}',  # unclosed tag
        "<div class='{{ x }}'",  # unclosed attribute
    ],
)
def test_unsupported_positions_are_rejected_at_parse_time(
    env: Environment, source: str
) -> None:
    with pytest.raises(HTMLContextError):
        env.from_string(source)


def test_conditional_branches_must_end_in_consistent_context(
    env: Environment,
) -> None:
    # One branch ends inside a tag, the other (implicit else) in an attribute.
    with pytest.raises(HTMLContextError):
        env.from_string('<div class="x{% if a %}"{% endif %}>')
    # Explicit branches disagree.
    with pytest.raises(HTMLContextError):
        env.from_string('{% if a %}<div class="{% else %}<b>{% endif %}')
    # Consistent branches are allowed.
    assert env.from_string("{% if a %}<b>{% else %}<i>{% endif %}x").render(a=True) == (
        "<b>x"
    )
    # Consistent branches inside an attribute are allowed.
    template = env.from_string('<div class="{% if a %}x{% else %}y{% endif %}">')
    assert template.render(a=False) == '<div class="y">'


def test_case_branches_must_end_in_consistent_context(env: Environment) -> None:
    with pytest.raises(HTMLContextError):
        env.from_string('{% case x %}{% when 1 %}<div class="{% else %}y{% endcase %}')
    template = env.from_string("{% case x %}{% when 1 %}<b>{% else %}<i>{% endcase %}")
    assert template.render(x=2) == "<i>"


def test_unless_branches_must_end_in_consistent_context(env: Environment) -> None:
    with pytest.raises(HTMLContextError):
        env.from_string('{% unless a %}<div class="{% endunless %}')
    assert env.from_string("{% unless a %}<b>{% endunless %}").render(a=True) == ""


def test_loop_body_must_be_balanced(env: Environment) -> None:
    with pytest.raises(HTMLContextError):
        env.from_string('{% for i in (1..3) %}<div class="{% endfor %}')
    # A balanced loop body is fine.
    template = env.from_string("{% for i in (1..2) %}<b>{{ i }}</b>{% endfor %}")
    assert template.render() == "<b>1</b><b>2</b>"
    # An unbalanced for/else branch is rejected.
    with pytest.raises(HTMLContextError):
        env.from_string('{% for i in x %}<b>{% else %}<i class="{% endfor %}')


def test_capture_block_must_be_balanced(env: Environment) -> None:
    with pytest.raises(HTMLContextError):
        env.from_string('{% capture c %}<div class="{% endcapture %}')
    # A balanced capture block can be output in text context.
    template = env.from_string("{% capture c %}<b>{{ y }}</b>{% endcapture %}{{ c }}")
    assert template.render(y="<i>") == "<b>&lt;i&gt;</b>"


def test_captured_fragment_is_escaped_in_attribute_context(env: Environment) -> None:
    # Even though captured content is marked "safe" for text context, it does
    # not get a free pass in attribute context.
    template = env.from_string(
        '{% capture c %}<b>{{ y }}</b>{% endcapture %}<div title="{{ c }}">'
    )
    assert template.render(y="x") == '<div title="&lt;b&gt;x&lt;/b&gt;">'


def test_include_participates_in_context_checks() -> None:
    loader = DictLoader(
        {
            "good": "<b>{{ y }}</b>",
            "bad": '<div class="unclosed',
        }
    )
    env = Environment(contextual_autoescape=True, loader=loader)

    # A self-contained partial can be included from text context.
    assert env.from_string("{% include 'good' %}").render(y="<i>") == (
        "<b>&lt;i&gt;</b>"
    )
    # A partial that is not self-contained is rejected when parsed.
    with pytest.raises(HTMLContextError):
        env.get_template("bad")
    # A partial can not be included from inside an attribute.
    with pytest.raises(HTMLContextError):
        env.from_string('<div class="{% include \'good\' %}">')


def test_render_tag_participates_in_context_checks() -> None:
    loader = DictLoader({"partial": "<b>{{ y }}</b>"})
    env = Environment(contextual_autoescape=True, loader=loader)
    assert env.from_string("{% render 'partial' %}").render(y="<i>") == (
        "<b>&lt;i&gt;</b>"
    )
    with pytest.raises(HTMLContextError):
        env.from_string('<div class="{% render \'partial\' %}">')


def test_render_rejects_unverified_inline_snippet() -> None:
    env = Environment(contextual_autoescape=True)
    # A template parsed by an environment without contextual autoescape has not
    # been verified, so it can not be rendered as an inline snippet.
    other = Environment()
    snippet = other.from_string("<b>{{ y }}</b>")
    template = env.from_string("{% render snippet %}")
    with pytest.raises(HTMLContextError):
        template.render(snippet=snippet)


def test_echo_tag_is_escaped_for_context(env: Environment) -> None:
    assert env.from_string('<div title="{% echo x %}">').render(x='a"b') == (
        '<div title="a&#34;b">'
    )
    with pytest.raises(HTMLContextError):
        env.from_string("<script>{% echo x %}</script>")


def test_cycle_tag_is_escaped_for_context(env: Environment) -> None:
    template = env.from_string(
        '<div class="{% cycle "a", "b\'" %}{% cycle "a", "b\'" %}">'
    )
    assert template.render() == '<div class="ab&#39;">'
    with pytest.raises(HTMLContextError):
        env.from_string("<style>{% cycle 'a', 'b' %}</style>")


def test_increment_and_decrement_positions_are_checked(env: Environment) -> None:
    assert env.from_string("<p>{% increment c %}</p>").render() == "<p>0</p>"
    assert env.from_string('<div title="{% increment c %}">').render() == (
        '<div title="0">'
    )
    with pytest.raises(HTMLContextError):
        env.from_string("<script>{% increment c %}</script>")


def test_output_split_across_tag_boundaries(env: Environment) -> None:
    # Literal text ending in a dangling "<" before an output statement is a
    # dynamic tag name, which is unsupported.
    with pytest.raises(HTMLContextError):
        env.from_string("<{{ x }}")
    # An output statement can not complete a half-open attribute either.
    with pytest.raises(HTMLContextError):
        env.from_string('<div class="a{% if x %}{% endif %}')  # unclosed
    # But output inside a well-formed quoted attribute, with control flow
    # around it, is fine.
    template = env.from_string('<div class="a{% if x %}{{ y }}{% endif %}z">')
    assert template.render(x=True, y='"') == '<div class="a&#34;z">'


def test_unknown_tags_are_rejected() -> None:
    env = Environment(contextual_autoescape=True, extra=True)
    with pytest.raises(HTMLContextError):
        env.from_string('{% macro "foo" %}x{% endmacro %}')


def test_plain_autoescape_does_not_do_contextual_checks() -> None:
    # The existing autoescape mode is unchanged: a single escape everywhere,
    # no URL protocol validation and no positional checks.
    env = Environment(autoescape=True)
    assert env.from_string('<a href="{{ u }}">').render(u="javascript:alert(1)") == (
        '<a href="javascript:alert(1)">'
    )
    assert env.from_string("<script>{{ x }}</script>").render(x="1<2") == (
        "<script>1&lt;2</script>"
    )


def test_no_autoescape_by_default() -> None:
    env = Environment()
    assert env.from_string('<div class="{{ x }}">').render(x='a"b') == (
        '<div class="a"b">'
    )


def test_async_render(env: Environment) -> None:
    async def run() -> None:
        template = env.from_string('<a href="{{ u }}">{{ x }}</a>')
        assert await template.render_async(u="https://x.io", x="<b>") == (
            '<a href="https://x.io">&lt;b&gt;</a>'
        )
        with pytest.raises(HTMLContextError):
            await template.render_async(u="javascript:alert(1)", x="y")

    asyncio.run(run())


def test_async_include() -> None:
    async def run() -> None:
        loader = DictLoader({"partial": "<b>{{ y }}</b>"})
        env = Environment(contextual_autoescape=True, loader=loader)
        template = env.from_string("{% include 'partial' %}")
        assert await template.render_async(y="<i>") == "<b>&lt;i&gt;</b>"

    asyncio.run(run())
