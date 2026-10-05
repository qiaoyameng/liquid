"""Tests for the optional ``message`` tag and ``MessageCatalog``."""

import asyncio

import pytest

from liquid import Environment
from liquid.extra import MessageCatalog
from liquid.extra import MessageCatalogError
from liquid.extra import MessageInterpolationError
from liquid.extra import MessageLookupError
from liquid.extra import MessageTag

CATALOG = MessageCatalog(
    {
        "cart.items": {
            "en": {
                "one": "You have %(count)s item",
                "other": "You have %(count)s items",
            },
            "ru": {
                "one": "%(count)s товар",
                "few": "%(count)s товара",
                "many": "%(count)s товаров",
                "other": "%(count)s товара",
            },
        },
        "welcome": {
            "en": "Hello, %(name)s!",
            "ru": "Привет, %(name)s!",
        },
    },
    default_locale="en",
    fallbacks={"ru": ["en"]},
)


def make_env(*, autoescape: bool = False) -> Environment:
    env = Environment(autoescape=autoescape)
    env.add_tag(MessageTag)
    return env


def test_english_plural_categories() -> None:
    template = make_env().from_string("{% message 'cart.items', count: n %}")
    assert template.render(message_catalog=CATALOG, n=1) == "You have 1 item"
    assert template.render(message_catalog=CATALOG, n=2) == "You have 2 items"
    assert template.render(message_catalog=CATALOG, n=5) == "You have 5 items"


def test_russian_plural_categories() -> None:
    template = make_env().from_string(
        "{% message 'cart.items', count: n, locale: 'ru' %}"
    )
    assert template.render(message_catalog=CATALOG, n=1) == "1 товар"
    assert template.render(message_catalog=CATALOG, n=3) == "3 товара"
    assert template.render(message_catalog=CATALOG, n=5) == "5 товаров"


def test_decimal_count_is_not_truncated() -> None:
    """A fractional count must not be rounded before plural form selection."""
    template = make_env().from_string("{% message 'cart.items', count: n %}")
    # English "one" requires an integer value of 1. If 1.5 were truncated to
    # 1, this would incorrectly select the "one" form.
    assert template.render(message_catalog=CATALOG, n=1.5) == "You have 1.5 items"
    # Russian fractions select the "other" form, not "one".
    assert (
        template.render(message_catalog=CATALOG, n=1.5, locale="ru")
        == "1.5 товара"
    )


def test_string_count_is_not_truncated() -> None:
    template = make_env().from_string("{% message 'cart.items', count: '1.5' %}")
    assert template.render(message_catalog=CATALOG) == "You have 1.5 items"


def test_message_id_from_variable() -> None:
    template = make_env().from_string("{% message msg_id, name: 'World' %}")
    assert (
        template.render(message_catalog=CATALOG, msg_id="welcome")
        == "Hello, World!"
    )


def test_locale_from_context_variable() -> None:
    template = make_env().from_string("{% message 'welcome', name: 'Мир' %}")
    assert (
        template.render(message_catalog=CATALOG, locale="ru") == "Привет, Мир!"
    )


def test_locale_kwarg_takes_precedence_over_context() -> None:
    template = make_env().from_string(
        "{% message 'welcome', name: 'World', locale: 'en' %}"
    )
    assert (
        template.render(message_catalog=CATALOG, locale="ru") == "Hello, World!"
    )


def test_fallback_locale_chain() -> None:
    catalog = MessageCatalog(
        {"welcome": {"en": "Hello, %(name)s!"}},
        default_locale="en",
        fallbacks={"ru": ["en"]},
    )
    template = make_env().from_string("{% message 'welcome', name: 'A' %}")
    assert template.render(message_catalog=catalog, locale="ru") == "Hello, A!"


def test_transitive_fallback_locale_chain() -> None:
    catalog = MessageCatalog(
        {"welcome": {"en": "Hello!"}},
        default_locale="en",
        fallbacks={"de-at": ["de"], "de": ["en"]},
    )
    assert catalog.locale_chain("de-at") == ("de-at", "de", "en")
    template = make_env().from_string("{% message 'welcome' %}")
    assert template.render(message_catalog=catalog, locale="de-at") == "Hello!"


def test_fallback_cycle_is_rejected() -> None:
    with pytest.raises(MessageCatalogError, match="cycle"):
        MessageCatalog({}, fallbacks={"ru": ["en"], "en": ["ru"]})


def test_fallback_self_cycle_is_rejected() -> None:
    with pytest.raises(MessageCatalogError, match="cycle"):
        MessageCatalog({}, fallbacks={"en": ["en"]})


def test_missing_message() -> None:
    template = make_env().from_string("{% message 'no.such.message' %}")
    with pytest.raises(MessageLookupError, match="no message 'no.such.message'"):
        template.render(message_catalog=CATALOG)


def test_missing_plural_form_falls_back_to_other() -> None:
    catalog = MessageCatalog({"n": {"ru": {"one": "one", "other": "other"}}})
    template = make_env().from_string("{% message 'n', count: 5, locale: 'ru' %}")
    assert template.render(message_catalog=catalog) == "other"


def test_missing_plural_form_without_other() -> None:
    catalog = MessageCatalog({"n": {"ru": {"one": "one"}}})
    template = make_env().from_string("{% message 'n', count: 5, locale: 'ru' %}")
    with pytest.raises(MessageLookupError, match="missing plural form 'many'"):
        template.render(message_catalog=catalog)


def test_missing_placeholder_value() -> None:
    template = make_env().from_string("{% message 'welcome' %}")
    with pytest.raises(
        MessageInterpolationError, match="missing placeholder value 'name'"
    ):
        template.render(message_catalog=CATALOG)


def test_unexpected_placeholder_value() -> None:
    template = make_env().from_string(
        "{% message 'welcome', name: 'A', extra: 1 %}"
    )
    with pytest.raises(
        MessageInterpolationError, match="unexpected placeholder value 'extra'"
    ):
        template.render(message_catalog=CATALOG)


def test_invalid_count() -> None:
    template = make_env().from_string("{% message 'cart.items', count: true %}")
    with pytest.raises(
        MessageInterpolationError, match="count must be a number"
    ):
        template.render(message_catalog=CATALOG)


def test_missing_catalog() -> None:
    template = make_env().from_string("{% message 'welcome', name: 'A' %}")
    with pytest.raises(MessageCatalogError, match="no message catalog"):
        template.render()


def test_catalog_from_tag_class_attribute() -> None:
    class MyMessageTag(MessageTag):
        catalog = CATALOG

    env = Environment()
    env.add_tag(MyMessageTag)
    template = env.from_string("{% message 'welcome', name: 'A' %}")
    assert template.render() == "Hello, A!"


def test_placeholder_values_are_escaped_with_autoescape() -> None:
    template = make_env(autoescape=True).from_string(
        "{% message 'welcome', name: name %}"
    )
    assert (
        template.render(message_catalog=CATALOG, name="<script>alert(1)</script>")
        == "Hello, &lt;script&gt;alert(1)&lt;/script&gt;!"
    )


def test_placeholder_values_are_not_escaped_without_autoescape() -> None:
    template = make_env().from_string("{% message 'welcome', name: name %}")
    assert (
        template.render(message_catalog=CATALOG, name="<b>Bob</b>")
        == "Hello, <b>Bob</b>!"
    )


def test_catalog_text_is_not_rendered_as_liquid() -> None:
    """Message catalog text must not be executed as a Liquid template."""
    catalog = MessageCatalog(
        {"evil": {"en": "{{ 7 }} {% if true %}x{% endif %} %(name)s"}}
    )
    template = make_env(autoescape=True).from_string(
        "{% message 'evil', name: 'N' %}"
    )
    assert (
        template.render(message_catalog=catalog)
        == "{{ 7 }} {% if true %}x{% endif %} N"
    )


def test_literal_percent_sign() -> None:
    catalog = MessageCatalog({"pct": {"en": "100%% sure, %(name)s"}})
    template = make_env().from_string("{% message 'pct', name: 'A' %}")
    assert template.render(message_catalog=catalog) == "100% sure, A"


def test_catalog_check_passes_for_valid_catalog() -> None:
    CATALOG.check()


def test_catalog_check_reports_missing_plural_categories() -> None:
    catalog = MessageCatalog(
        {"items": {"ru": {"one": "%(count)s", "other": "%(count)s"}}}
    )
    with pytest.raises(MessageCatalogError, match="missing plural") as exc_info:
        catalog.check()
    assert "'few'" in str(exc_info.value)
    assert "'many'" in str(exc_info.value)


def test_catalog_check_reports_inconsistent_placeholders() -> None:
    catalog = MessageCatalog(
        {
            "items": {
                "en": {
                    "one": "%(count)s item for %(name)s",
                    "other": "%(count)s items",
                }
            }
        }
    )
    with pytest.raises(MessageCatalogError, match="do not match"):
        catalog.check()


def test_catalog_check_reports_unknown_locale() -> None:
    catalog = MessageCatalog({"items": {"not-a-locale": "hi"}})
    with pytest.raises(MessageCatalogError, match="unknown locale"):
        catalog.check()


def test_catalog_check_reports_malformed_placeholder() -> None:
    catalog = MessageCatalog({"items": {"en": "hello %(name"}})
    with pytest.raises(MessageCatalogError, match="malformed placeholder"):
        catalog.check()


def test_render_message_async() -> None:
    async def coro() -> str:
        env = make_env(autoescape=True)
        template = env.from_string(
            "{% message 'cart.items', count: n, locale: 'ru' %}"
        )
        return await template.render_async(message_catalog=CATALOG, n=1.5)

    assert asyncio.run(coro()) == "1.5 товара"
