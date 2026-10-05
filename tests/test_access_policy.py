from __future__ import annotations

import pytest

from liquid import Environment
from liquid.access import AccessDecision
from liquid.access import PathAccessPolicy
from liquid.exceptions import AccessDeniedError
from liquid.filter import trusted_filter


class GuardedObject:
    calls = 0

    def __init__(self, value: str):
        self.value = value
        self.calls = 0

    def __getitem__(self, key: str) -> str:
        if key == "secret":
            type(self).calls += 1
            self.calls += 1
            return self.value
        if key == "public":
            return "public"
        raise KeyError(key)


class GuardedItem:
    calls = 0

    def __init__(self, public: str = "p", secret: str = "s"):
        self.public = public
        self.secret = secret
        self.calls = 0

    def __getitem__(self, key: str) -> str:
        type(self).calls += 1
        self.calls += 1
        if key == "public":
            return self.public
        if key == "secret":
            return self.secret
        raise KeyError(key)


def reset_calls() -> None:
    GuardedObject.calls = 0
    GuardedItem.calls = 0


@pytest.fixture(autouse=True)
def reset():
    reset_calls()
    yield
    reset_calls()


def test_denied_direct_loop_and_filter_paths_do_not_invoke_getter():
    env = Environment(
        access_policy=PathAccessPolicy(
                {
                    "user": (
                        "public",
                        ("items", "*", "public"),
                        ("items", "*", "nested", "public"),
                    )
                }
        )
    )
    data = {
        "user": {
            "public": "ok",
            "secret": GuardedObject("secret-value"),
            "items": [GuardedItem()],
        },
        "key": "secret",
    }

    assert env.from_string("{{ user.secret }}").render(**data) == ""
    assert GuardedObject.calls == 0

    assert (
        env.from_string(
            "{% for item in user.items %}{{ item.secret }}{% endfor %}"
        ).render(**data)
        == ""
    )
    assert GuardedItem.calls == 0

    assert env.from_string("{{ user.items | map: 'secret' }}").render(**data) == ""
    assert GuardedItem.calls == 0

    assert env.from_string("{{ user.items | where: 'secret' }}").render(**data) == ""
    assert GuardedItem.calls == 0

    nested = {"public": "n", "secret": GuardedObject("nested-secret")}
    assert (
        env.from_string(
            "{{ user.items | map: 'nested' | map: 'secret' }}"
        ).render(
            user={"items": [{"nested": nested}]}
        )
        == ""
    )
    assert GuardedObject.calls == 0
    assert (
        env.from_string(
            "{{ user.items | map: 'nested' | map: 'public' | join: ',' }}"
        ).render(
            user={"items": [{"nested": nested}]}
        )
        == "n"
    )

    assert env.from_string("{{ user[key] }}").render(**data) == ""
    assert GuardedObject.calls == 0

    assert (
        env.from_string(
            "{% for item in user.items %}{{ item.public }}{% endfor %}"
        ).render(**data)
        == "p"
    )
    assert GuardedItem.calls == 1


def test_denied_result_can_raise_without_value_in_message():
    env = Environment(
        access_policy=PathAccessPolicy(
            {"user": (("items", "*", "public"),)},
            on_denied="raise",
            denied_message="access denied",
        )
    )
    data = {"user": {"items": [GuardedItem(secret="secret-value")]}}

    with pytest.raises(AccessDeniedError, match="access denied") as exc:
        env.from_string("{{ user.items | map: 'secret' }}").render(**data)

    assert "secret-value" not in str(exc.value)
    assert GuardedItem.calls == 0

    with pytest.raises(AccessDeniedError, match="access denied"):
        env.from_string("{{ user.items }}").render(**data)


def test_custom_filter_must_declare_trusted_boundary():
    received = []

    def untrusted_filter(value):
        received.append(value)
        return "leaked"

    env = Environment(
        access_policy=PathAccessPolicy(
            {"user": (("items", "*", "public"),)}
        )
    )
    env.add_filter("untrusted", untrusted_filter)
    data = {"user": {"items": [GuardedItem(secret="secret-value")]}}

    assert env.from_string("{{ user.items | untrusted }}").render(**data) == ""
    assert received == []

    def untrusted_argument(left: object, argument: object) -> str:
        received.append(argument)
        return "leaked"

    env.add_filter("untrusted_argument", untrusted_argument)
    assert (
        env.from_string("{{ 'x' | untrusted_argument: user }}").render(**data) == ""
    )
    assert received == []

    @trusted_filter
    def trusted(value):
        return "trusted"

    env.add_filter("trusted_filter_under_test", trusted)
    assert (
        env.from_string("{{ user.items | trusted_filter_under_test }}").render(**data)
        == ""
    )


def test_policy_decisions_support_wildcards_and_traversal():
    policy = PathAccessPolicy({"user": (("items", "*", "public"),)})

    assert policy.decide("user", ()) is AccessDecision.TRAVERSE
    assert policy.decide("user", ("items",)) is AccessDecision.TRAVERSE
    assert policy.decide("user", ("items", 0)) is AccessDecision.TRAVERSE
    assert policy.decide("user", ("items", 0, "public")) is AccessDecision.ALLOW
    assert policy.decide("user", ("items", 0, "secret")) is AccessDecision.DENY
    assert policy.decide("other", ()) is AccessDecision.DENY


def test_policy_state_is_independent_per_render():
    env = Environment(
        access_policy=PathAccessPolicy({"user": (("items", "*", "public"),)})
    )
    template = env.from_string(
        "{% for item in user.items %}{{ item.public }}{% endfor %}"
    )
    first_item = GuardedItem(public="first")
    second_item = GuardedItem(public="second")
    first_data = {"items": [first_item]}
    second_data = {"items": [second_item]}

    assert template.render(user=first_data) == "first"
    assert template.render(user=first_data) == "first"
    assert template.render(user=second_data) == "second"
    assert first_item.calls == 2
    assert second_item.calls == 1


def test_render_state_is_isolated_from_template_and_env():
    env = Environment(
        access_policy=PathAccessPolicy({"user": (("items", "*", "public"),)})
    )
    template = env.from_string("{% for item in user.items %}{{ item.public }}{% endfor %}")
    first_item = GuardedItem(public="first")
    second_item = GuardedItem(public="second")

    assert template.render(user={"items": [first_item]}) == "first"
    assert template.render(user={"items": [second_item]}) == "second"
    assert first_item.calls == 1
    assert second_item.calls == 1
