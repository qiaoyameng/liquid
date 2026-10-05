import asyncio
from liquid import Environment, DictLoader

partials = {
    "email_card": (
        "<div class=\"card\">"
        "<h1>{% yield header %}</h1>"
        "<p>{% yield body %}default body{% endyield %}</p>"
        "<footer>{% yield footer %}</footer>"
        "<h1>{% yield header %}</h1>"
        "</div>"
    ),
    "nested_outer": (
        "{% assign x = 'OUTERLOCAL' %}"
        "OUTER({{ x }}"
        "{% component 'nested_inner' c: 'innerval' %}"
        "{% slot inner_slot %}inner-slot:{{ x }}{% endslot %}"
        "{% endcomponent %}"
        ")"
    ),
    "nested_inner": (
        "INNER[c={{ c }}]{% yield inner_slot %}"
    ),
}

env = Environment(extra=True, loader=DictLoader(partials))

source = (
    "{% assign x = 'caller-x' %}"
    "{% component 'email_card' %}"
    "{% slot header %}H-{{ x }}{% endslot %}"
    "{% slot body %}B-{{ x }}{% endslot %}"
    "{% endcomponent %}"
)
out = env.render(source)
print("1:", out)
assert out == ('<div class="card"><h1>H-caller-x</h1><p>B-caller-x</p>'
               '<footer></footer><h1>H-caller-x</h1></div>'), out

out = env.render("{% component 'email_card' %}{% slot header %}H{% endslot %}{% endcomponent %}")
print("2:", out)
assert "default body" in out

partials["scoped"] = (
    "arg={{ a }}|local={{ local | default: 'unset' }}|"
    "{% assign local = 'inside' %}after={{ local }}"
)
out = env.render(
    "{% component 'scoped' a: 42 %}{% endcomponent %}outside={{ local | default: 'unset' }}"
)
print("3:", out)
assert out == "arg=42|local=unset|after=insideoutside=unset", out

partials["twice"] = "{{ g }}|{% yield s %}|{% yield s %}"
out = env.render(
    "{% assign g = 'G' %}{% component 'twice' g: 'ignored' %}"
    "{% slot s %}{{ g }}{% assign g = 'CHANGED' %}{% endslot %}"
    "{% endcomponent %}after={{ g }}"
)
print("4:", out)
assert out == "ignored|G|Gafter=G", out

partials["params"] = "{% yield s item: foo %}"
out = env.render(
    "{% assign foo = 'caller-foo' %}{% component 'params' foo: 'comp-foo' %}"
    "{% slot s %}{{ item }}|{{ foo }}{% endslot %}{% endcomponent %}"
)
print("5:", out)
assert out == "comp-foo|caller-foo", out

partials["unknown"] = "[{% yield nope %}][{% yield nope2 %}fb{% endyield %}]"
out = env.render("{% component 'unknown' %}{% endcomponent %}")
print("6:", out)
assert out == "[][fb]", out

partials["dup"] = "{% yield s %}"
out = env.render(
    "{% component 'dup' %}{% slot s %}first{% endslot %}"
    "{% slot s %}second{% endslot %}{% endcomponent %}"
)
print("7:", out)
assert out == "second", out

partials["card_default"] = "{% yield %}|{% yield body %}x{% endyield %}"
out = env.render("{% component 'card_default' %}hello {{ x }}{% endcomponent %}")
print("8:", out)
assert out == "hello |x", out

out = env.render("{% assign x = 'TOPX' %}"
    "{% component 'nested_outer' %}{% endcomponent %}")
print("9:", out)
# Slot keeps the lexical (nested_outer) scope, not the top-level caller.
assert out == "OUTER(OUTERLOCALINNER[c=innerval]inner-slot:OUTERLOCAL)", out

env_auto = Environment(extra=True, autoescape=True, loader=DictLoader(partials))
out = env_auto.render("{% component 'twice' g: 1 %}"
    "{% slot s %}{{ v }}{% endslot %}{% endcomponent %}", v="<b>")
print("10:", out)
assert out == "1|&lt;b&gt;|&lt;b&gt;", out
# Markup captured via assign is not double-escaped, same as normal fragments.
frag = env_auto.render("{% assign x = v %}{{ x }}", v="<b>")
out2 = env_auto.render("{% component 'twice' g: 1 %}"
    "{% slot s %}{% assign x = v %}{{ x }}{% endslot %}{% endcomponent %}", v="<b>")
assert out2 == "1|" + frag + "|" + frag, (frag, out2)

from liquid.exceptions import ContextDepthError
partials["inf"] = "{% component 'inf' %}{% endcomponent %}"
try:
    env.render("{% component 'inf' %}{% endcomponent %}")
    raise SystemExit("expected ContextDepthError")
except ContextDepthError:
    print("11: ContextDepthError raised")

async def main():
    return await env.render_async(
        "{% assign x = 'caller-x' %}{% component 'email_card' %}"
        "{% slot header %}H-{{ x }}{% endslot %}"
        "{% slot body %}B-{{ x }}{% endslot %}{% endcomponent %}"
    )
a = asyncio.run(main())
print("12:", a)
assert "H-caller-x" in a

# 13. nested component chain with argument passing
partials["r0"] = "base[{% component 'r1' a: 1 %}{% endcomponent %}]"
partials["r1"] = "r1({{ a }})[{% component 'r2' b: a %}{% endcomponent %}]"
partials["r2"] = "r2({{ b }})"
out = env.render("{% component 'r0' %}{% endcomponent %}")
print("13:", out)
assert out == "base[r1(1)[r2(1)]]", out

# 14. yield default content may contain other tags, including nested yields
partials["defn"] = "{% yield s %}{% if true %}IF{% endif %}{% endyield %}|{% yield s %}"
out = env.render("{% component 'defn' %}{% endcomponent %}")
print("14:", out)
assert out == "IF|", out

# 15. slot name as quoted string and bare word both work
partials["named"] = "[{% yield 'a' %}][{% yield b %}]"
out = env.render("{% component 'named' %}{% slot 'a' %}A{% endslot %}{% slot b %}B{% endslot %}{% endcomponent %}")
print("15:", out)
assert out == "[A][B]", out

# 16. component name can be a variable resolving to a string
out = env.render("{% assign which = 'named' %}{% component which %}{% slot a %}A{% endslot %}{% slot b %}B{% endslot %}{% endcomponent %}")
print("16:", out)
assert out == "[A][B]", out

print("ALL OK")
