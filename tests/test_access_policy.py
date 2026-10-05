"""Tests for `liquid.policy.AccessPolicy`."""

import io

import pytest

from liquid import AccessPolicy
from liquid import DeniedAccessError
from liquid import Environment
from liquid import RenderContext
from liquid.filter import trusted_filter
from liquid.policy import GuardedValue
from liquid.policy import RestrictedUndefined


class RecordingUser:
    """A drop-like object with a call-recording getter on `secret`."""

    def __init__(self, name: str, role: str = "user", secret: str = "s3cr3t"):
        self.name = name
        self.role = role
        self.secret_calls = 0
        self._secret = secret

    @property
    def secret(self) -> str:
        self.secret_calls += 1
        return self._secret

    def __getitem__(self, key: str) -> object:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None


def test_allowed_direct_access() -> None:
    env = Environment(access_policy=AccessPolicy(allow={"user": ["name"]}))
    user = RecordingUser("alice")
    assert env.from_string("{{ user.name }}").render(user=user) == "alice"


def test_allowed_secret_calls_getter_once() -> None:
    env = Environment(access_policy=AccessPolicy(allow={"user": ["*"]}))
    user = RecordingUser("alice")
    assert env.from_string("{{ user.secret }}").render(user=user) == "s3cr3t"
    assert user.secret_calls == 1


def test_denied_direct_access_raises_without_calling_getter() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    user = RecordingUser("alice")
    with pytest.raises(DeniedAccessError):
        env.from_string("{{ user.secret }}").render(user=user)
    assert user.secret_calls == 0


def test_denied_direct_access_as_restricted_undefined() -> None:
    env = Environment(
        access_policy=AccessPolicy(
            deny={"user": ["secret"]}, on_denial="undefined"
        )
    )
    user = RecordingUser("alice")
    template = env.from_string("{{ user.secret }}")
    assert template.render(user=user) == ""
    assert user.secret_calls == 0

    context = RenderContext(template, globals={"user": user})
    value = context.get(["user", "secret"], token=None)
    assert isinstance(value, RestrictedUndefined)
    assert str(value) == ""


def test_denied_loop_alias_access_does_not_call_getter() -> None:
    env = Environment(
        access_policy=AccessPolicy(allow={"users": ["*.name", "*.role"]})
    )
    users = [RecordingUser("alice", role="admin"), RecordingUser("bob")]
    template = env.from_string("{% for u in users %}{{ u.secret }}{% endfor %}")
    with pytest.raises(DeniedAccessError):
        template.render(users=users)
    assert [u.secret_calls for u in users] == [0, 0]


def test_allowed_loop_alias_access() -> None:
    env = Environment(
        access_policy=AccessPolicy(allow={"users": ["*.name", "*.role"]})
    )
    users = [RecordingUser("alice", role="admin"), RecordingUser("bob")]
    template = env.from_string("{% for u in users %}{{ u.name }} {% endfor %}")
    assert template.render(users=users) == "alice bob "


def test_denied_map_filter_access_does_not_call_getter() -> None:
    env = Environment(
        access_policy=AccessPolicy(allow={"users": ["*.name", "*.role"]})
    )
    users = [RecordingUser("alice", role="admin"), RecordingUser("bob")]
    template = env.from_string("{{ users | map: 'secret' | join: ',' }}")
    with pytest.raises(DeniedAccessError):
        template.render(users=users)
    assert [u.secret_calls for u in users] == [0, 0]


def test_denied_map_filter_access_as_undefined() -> None:
    env = Environment(
        access_policy=AccessPolicy(
            allow={"users": ["*.name"]}, on_denial="undefined"
        )
    )
    users = [RecordingUser("alice"), RecordingUser("bob")]
    template = env.from_string("{{ users | map: 'secret' | join: ',' }}")
    assert template.render(users=users) == ","
    assert [u.secret_calls for u in users] == [0, 0]


def test_where_filter_preserves_provenance() -> None:
    env = Environment(
        access_policy=AccessPolicy(allow={"users": ["*.name", "*.role"]})
    )
    users = [RecordingUser("alice", role="admin"), RecordingUser("bob")]
    template = env.from_string(
        "{% assign admins = users | where: 'role', 'admin' %}"
        "{% for u in admins %}{{ u.secret }}{% endfor %}"
    )
    with pytest.raises(DeniedAccessError):
        template.render(users=users)
    assert [u.secret_calls for u in users] == [0, 0]


def test_denied_dynamic_key_access() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    user = RecordingUser("alice")
    template = env.from_string("{% assign key = 'secret' %}{{ user[key] }}")
    with pytest.raises(DeniedAccessError):
        template.render(user=user)
    assert user.secret_calls == 0


def test_denied_index_access() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"users": ["*.secret"]}))
    users = [RecordingUser("alice")]
    template = env.from_string("{{ users[0].secret }}")
    with pytest.raises(DeniedAccessError):
        template.render(users=users)
    assert users[0].secret_calls == 0


def test_assign_preserves_provenance() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    user = RecordingUser("alice")
    template = env.from_string("{% assign u = user %}{{ u.secret }}")
    with pytest.raises(DeniedAccessError):
        template.render(user=user)
    assert user.secret_calls == 0


def test_error_message_does_not_include_value() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    user = RecordingUser("alice", secret="hunter2")
    with pytest.raises(DeniedAccessError) as exc_info:
        env.from_string("{{ user.secret }}").render(user=user)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "user.secret" in message


def test_deny_applies_to_subtree() -> None:
    env = Environment(access_policy=AccessPolicy(deny={"user": ["address"]}))
    user = {"name": "alice", "address": {"city": "London"}}
    with pytest.raises(DeniedAccessError):
        env.from_string("{{ user.address.city }}").render(user=user)


def test_default_deny_for_unlisted_roots() -> None:
    env = Environment(
        access_policy=AccessPolicy(
            allow={"user": ["name"]}, default_allow=False, on_denial="undefined"
        )
    )
    template = env.from_string("{{ user.name }}{{ other }}")
    assert template.render(user={"name": "alice"}, other="x") == "alice"


def test_untrusted_filter_receives_guarded_value() -> None:
    seen = {}

    def probe(value: object) -> str:
        seen["guarded"] = isinstance(value, GuardedValue)
        return "ok"

    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    env.add_filter("probe", probe)
    template = env.from_string("{{ user | probe }}")
    assert template.render(user={"secret": "x"}) == "ok"
    assert seen["guarded"] is True


def test_trusted_filter_receives_raw_object() -> None:
    seen = {}

    def probe(value: object) -> str:
        seen["guarded"] = isinstance(value, GuardedValue)
        seen["value"] = value
        return "ok"

    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    env.add_filter("probe", probe, trusted=True)
    template = env.from_string("{{ user | probe }}")
    assert template.render(user={"secret": "x"}) == "ok"
    assert seen["guarded"] is False
    assert seen["value"] == {"secret": "x"}


def test_trusted_filter_decorator() -> None:
    seen = {}

    @trusted_filter
    def probe(value: object) -> str:
        seen["guarded"] = isinstance(value, GuardedValue)
        return "ok"

    env = Environment(access_policy=AccessPolicy(deny={"user": ["secret"]}))
    env.add_filter("probe", probe)
    template = env.from_string("{{ user | probe }}")
    assert template.render(user={"secret": "x"}) == "ok"
    assert seen["guarded"] is False


def test_policy_state_is_independent_per_render() -> None:
    env = Environment(
        access_policy=AccessPolicy(
            deny={"user": ["secret"]}, on_denial="undefined"
        )
    )
    template = env.from_string("{{ user.secret }}")
    user = {"secret": "x"}

    buf1 = io.StringIO()
    context1 = RenderContext(template, globals={"user": user})
    template.render_with_context(context1, buf1)
    assert context1.access_policy_state is not None
    assert context1.access_policy_state.denials == ["user.secret"]

    buf2 = io.StringIO()
    context2 = RenderContext(template, globals={"user": user})
    template.render_with_context(context2, buf2)
    assert context2.access_policy_state is not None
    assert context2.access_policy_state is not context1.access_policy_state
    assert context2.access_policy_state.denials == ["user.secret"]
    # The first render's state is unchanged by the second render.
    assert context1.access_policy_state.denials == ["user.secret"]


def test_no_policy_means_no_state() -> None:
    env = Environment()
    template = env.from_string("{{ user.name }}")
    context = RenderContext(template, globals={"user": {"name": "alice"}})
    assert context.access_policy_state is None
    buf = io.StringIO()
    template.render_with_context(context, buf)
    assert buf.getvalue() == "alice"


def test_guarded_output_redacts_denied_fields() -> None:
    env = Environment(access_policy=AccessPolicy(allow={"user": ["name"]}))
    user = {"name": "alice", "secret": "hunter2"}
    result = env.from_string("{{ user }}").render(user=user)
    assert "alice" in result
    assert "hunter2" not in result
