"""Test cases for the optional `message` tag and its message catalog."""

from __future__ import annotations

import unittest
from decimal import Decimal

from liquid import Environment
from liquid.exceptions import LiquidSyntaxError
from liquid.exceptions import LiquidTypeError
from liquid.exceptions import TranslationError
from liquid.extra import MessageCatalogError
from liquid.extra import MessageTag
from liquid.extra.message_catalog import fallback_chain
from liquid.extra.message_catalog import format_message
from liquid.extra.message_catalog import plural_category
from liquid.extra.message_catalog import validate_fallback_chain

CATALOG = {
    "en": {
        "greeting": "Hello, {name}!",
        "apples": {
            "one": "{count} apple",
            "other": "{count} apples",
        },
    },
    "ru": {
        "apples": {
            "one": "{count} яблоко",
            "few": "{count} яблока",
            "many": "{count} яблок",
            "other": "{count} яблока",
        },
    },
}

FALLBACKS = {"ru": ["en"]}


class ApplesTag(MessageTag):
    catalog = CATALOG
    fallback_chain_map = FALLBACKS
    default_locale = "en"


def make_env(*, autoescape: bool = False, tag: type[MessageTag] = ApplesTag):
    env = Environment(autoescape=autoescape)
    env.add_tag(tag)
    return env


class PluralCategoryTestCase(unittest.TestCase):
    def test_english(self) -> None:
        expected = [
            (0, "other"),
            (1, "one"),
            (2, "other"),
            (21, "other"),
            (1.0, "one"),
            (Decimal("1.0"), "other"),
        ]
        for number, category in expected:
            self.assertEqual(plural_category("en", number), category)

    def test_russian(self) -> None:
        expected = [
            (1, "one"),
            (21, "one"),
            (2, "few"),
            (3, "few"),
            (5, "many"),
            (11, "many"),
            (0, "many"),
            (1.5, "other"),
            (Decimal("1.0"), "other"),
            (Decimal("1.50"), "other"),
            (Decimal("0"), "many"),
        ]
        for number, category in expected:
            self.assertEqual(
                plural_category("ru", number),
                category,
                f"unexpected category for {number!r}",
            )

    def test_decimal_string_is_not_rounded(self) -> None:
        # "1.0" must not be treated as the integer 1.
        self.assertEqual(plural_category("en", "1.0"), "other")
        self.assertEqual(plural_category("en", "1"), "one")
        self.assertEqual(plural_category("en", Decimal("1.00")), "other")

    def test_bool_is_not_a_count(self) -> None:
        with self.assertRaises(MessageCatalogError):
            plural_category("en", True)


class RenderTestCase(unittest.TestCase):
    def test_english_plural(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        self.assertEqual(template.render(n=1, locale="en"), "1 apple")
        self.assertEqual(template.render(n=0, locale="en"), "0 apples")
        self.assertEqual(template.render(n=2, locale="en"), "2 apples")

    def test_english_decimal_count_is_not_rounded(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        self.assertEqual(template.render(n=Decimal("1.0"), locale="en"), "1.0 apples")
        self.assertEqual(template.render(n=Decimal("1.5"), locale="en"), "1.5 apples")
        self.assertEqual(template.render(n="2.0", locale="en"), "2.0 apples")

    def test_russian_plural(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        expected = {
            1: "1 яблоко",
            2: "2 яблока",
            5: "5 яблок",
            11: "11 яблок",
            21: "21 яблоко",
        }
        for number, text in expected.items():
            self.assertEqual(template.render(n=number, locale="ru"), text)

    def test_russian_decimal_count(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        self.assertEqual(
            template.render(n=Decimal("1.5"), locale="ru"), "1.5 яблока"
        )
        self.assertEqual(
            template.render(n=Decimal("1.0"), locale="ru"), "1.0 яблока"
        )

    def test_locale_keyword_argument(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: 5, locale: 'ru' %}")
        self.assertEqual(template.render(), "5 яблок")

    def test_fallback_to_default_locale(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'greeting', name: 'World' %}")
        # 'ru' does not translate 'greeting', fall back to 'en'.
        self.assertEqual(template.render(locale="ru"), "Hello, World!")

    def test_named_placeholder(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'greeting', name: who %}")
        self.assertEqual(template.render(who="World", locale="en"), "Hello, World!")

    def test_html_escaped_values(self) -> None:
        env = make_env(autoescape=True)
        template = env.from_string("{% message 'greeting', name: who %}")
        result = template.render(who="<b>x</b>", locale="en")
        self.assertEqual(result, "Hello, &lt;b&gt;x&lt;/b&gt;!")

    def test_catalog_markup_is_not_executed(self) -> None:
        # Values containing Liquid tags are escaped as text and never
        # interpreted as template markup.
        env = make_env(autoescape=True)
        template = env.from_string("{% message 'greeting', name: who %}")
        result = template.render(
            who="{% if true %}<b>bad</b>{% endif %}", locale="en"
        )
        self.assertIn("{% if true %}", result)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", result)
        self.assertNotIn("<b>bad</b>", result)

    def test_literal_braces(self) -> None:
        class Tag(ApplesTag):
            catalog = {
                "en": {"braces": "use {{name}} for a literal brace, {name}"}
            }

        env = make_env(tag=Tag)
        result = env.from_string(
            "{% message 'braces', name: 'x' %}"
        ).render(locale="en")
        self.assertEqual(result, "use {name} for a literal brace, x")

    def test_context_supplied_catalog(self) -> None:
        class BareTag(MessageTag):
            default_locale = "en"

        env = make_env(tag=BareTag)
        template = env.from_string("{% message 'greeting', name: 'x' %}")
        result = template.render(
            locale="en",
            message_catalog={"en": {"greeting": "Hi, {name}."}},
        )
        self.assertEqual(result, "Hi, x.")


class ErrorTestCase(unittest.TestCase):
    def test_missing_message_id_argument(self) -> None:
        env = make_env()
        with self.assertRaises(LiquidSyntaxError):
            env.from_string("{% message %}")

    def test_message_id_must_be_string_literal(self) -> None:
        env = make_env()
        with self.assertRaises(LiquidSyntaxError):
            env.from_string("{% message some_var, name: 'x' %}")

    def test_unknown_message(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'nope', x: 1 %}")
        with self.assertRaises(TranslationError):
            template.render(locale="en")

    def test_missing_placeholder_value(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'greeting' %}")
        with self.assertRaises(TranslationError):
            template.render(locale="en")

    def test_extra_placeholder_value(self) -> None:
        env = make_env()
        template = env.from_string(
            "{% message 'greeting', name: 'x', other: 'y' %}"
        )
        with self.assertRaises(TranslationError):
            template.render(locale="en")

    def test_count_does_not_count_as_extra(self) -> None:
        env = make_env()
        # 'greeting' does not reference {count}; passing count is fine.
        result = env.from_string(
            "{% message 'greeting', name: 'x', count: 3 %}"
        ).render(locale="en")
        self.assertEqual(result, "Hello, x!")

    def test_invalid_count_type(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        with self.assertRaises(TranslationError):
            template.render(n=[1], locale="en")

    def test_missing_locale(self) -> None:
        class Tag(MessageTag):
            catalog = CATALOG

        env = make_env(tag=Tag)
        template = env.from_string("{% message 'greeting', name: 'x' %}")
        with self.assertRaises(LiquidTypeError):
            template.render()

    def test_no_catalog_configured(self) -> None:
        class Tag(MessageTag):
            pass

        env = make_env(tag=Tag)
        template = env.from_string("{% message 'greeting', name: 'x' %}")
        with self.assertRaises(TranslationError):
            template.render(locale="en")

    def test_missing_plural_category_at_render(self) -> None:
        env3 = Environment()
        env3.tags["message"] = MessageTag(
            env3,
            catalog={"en": {"m": {"one": "{count}"}}},
            default_locale="en",
            validate=False,
        )
        with self.assertRaises(TranslationError):
            env3.from_string("{% message 'm', count: 2 %}").render(locale="en")


class CatalogCheckTestCase(unittest.TestCase):
    def test_missing_plural_category(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={
                    "en": {"m": {"one": "{count}"}},
                },
                default_locale="en",
            )

    def test_unknown_category(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={
                    "en": {
                        "m": {
                            "one": "{count}",
                            "other": "{count}",
                            "banana": "{count}",
                        }
                    },
                },
                default_locale="en",
            )

    def test_shape_mismatch(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={
                    "en": {"m": "one"},
                    "ru": {"m": {"one": "1", "other": "2"}},
                },
                default_locale="en",
            )

    def test_placeholder_mismatch(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={
                    "en": {"m": {"one": "{a}", "other": "{a}"}},
                    "ru": {"m": {"one": "{b}", "other": "{b}"}},
                },
                default_locale="en",
            )

    def test_unsupported_placeholder_syntax(self) -> None:
        for text in ("{}", "{0}", "{a!r}", "{a:>3}", "{a.b}", "{a[0]}"):
            with self.subTest(text=text), self.assertRaises(MessageCatalogError):
                ApplesTag(
                    Environment(),
                    catalog={"en": {"m": text}},
                    default_locale="en",
                )

    def test_fallback_cycle_rejected(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog=CATALOG,
                fallback_chain_map={"en": ["ru"], "ru": ["en"]},
            )

    def test_default_locale_missing_from_catalog(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={"en": {"m": "x"}},
                default_locale="de",
            )


class FallbackChainTestCase(unittest.TestCase):
    def test_transitive_chain_deduped(self) -> None:
        chain = {"ru": ["en"], "uk": ["ru", "en"], "en": []}
        self.assertEqual(
            fallback_chain("uk", chain, default_locale="en"),
            ["uk", "ru", "en"],
        )

    def test_default_locale_appended(self) -> None:
        self.assertEqual(
            fallback_chain("de", {}, default_locale="en"), ["de", "en"]
        )

    def test_self_cycle(self) -> None:
        with self.assertRaises(MessageCatalogError):
            validate_fallback_chain({"en": ["en"]})

    def test_indirect_cycle(self) -> None:
        with self.assertRaises(MessageCatalogError):
            validate_fallback_chain({"a": ["b"], "b": ["c"], "c": ["a"]})

    def test_non_string_fallback_rejected(self) -> None:
        with self.assertRaises(MessageCatalogError):
            validate_fallback_chain({"en": [1]})  # type: ignore[dict-item]


class FormatMessageTestCase(unittest.TestCase):
    def test_autoescape(self) -> None:
        result = format_message(
            "Hi {name}", {"name": "<b>&</b>"}, autoescape=True
        )
        self.assertEqual(result, "Hi &lt;b&gt;&amp;&lt;/b&gt;")

    def test_no_autoescape(self) -> None:
        result = format_message("Hi {name}", {"name": "<b>"}, autoescape=False)
        self.assertEqual(result, "Hi <b>")

    def test_literal_braces(self) -> None:
        self.assertEqual(
            format_message("{{ {x} }}", {"x": "1"}), "{ 1 }"
        )


class FallbackRenderTestCase(unittest.TestCase):
    def test_plural_category_comes_from_resolved_locale(self) -> None:
        class Tag(ApplesTag):
            fallback_chain_map = {"fr": ["ru"]}

        env = make_env(tag=Tag)
        # French falls back to Russian; 5 -> Russian 'many'.
        result = env.from_string(
            "{% message 'apples', count: 5 %}"
        ).render(locale="fr")
        self.assertEqual(result, "5 яблок")

    def test_negative_count(self) -> None:
        env = make_env()
        template = env.from_string("{% message 'apples', count: n %}")
        self.assertEqual(template.render(n=-1, locale="ru"), "-1 яблоко")
        self.assertEqual(template.render(n=-3, locale="ru"), "-3 яблока")

    def test_default_count_is_zero(self) -> None:
        env = make_env()
        result = env.from_string("{% message 'apples' %}").render(locale="en")
        self.assertEqual(result, "0 apples")

    def test_invalid_locale_key_rejected(self) -> None:
        with self.assertRaises(MessageCatalogError):
            ApplesTag(
                Environment(),
                catalog={"xx-yy-zz": {"m": "x"}, "en": {"m": "y"}},
                default_locale="en",
            )

    def test_count_string_preserves_fraction(self) -> None:
        env = make_env()
        result = env.from_string(
            "{% message 'apples', count: n %}"
        ).render(n="2.50", locale="en")
        self.assertEqual(result, "2.50 apples")


if __name__ == "__main__":
    unittest.main()
