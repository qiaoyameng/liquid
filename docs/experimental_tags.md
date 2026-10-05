The tags described on this page are considered experimental and might change without warning and not follow semantic versioning.

## snippet

**_New in version 2.2.0_**

```plain
{% snippet <identifier> %}
  <liquid markup>
{% endsnippet %}
```

A snippet is a reusable block of Liquid markup. Traditionally we'd save a snippet to a file and include it in a template with the `{% render %}` tag.

```liquid
{% render  "some_snippet" %}
```

With the `{% snippet %}` tag we can define blocks for reuse inside a single template, potentially reducing the number of snippet files we need.

```liquid
{% snippet div %}
  <div>
    {{ content }}
  </div>
{% endsnippet %}
```

Defining a snippet does not render it. We use `{% render snippet_name %}` to render a snippet, where `snippet_name` is the name of your snippet without quotes (file-based snippet names must be quoted).

```liquid
{% snippet div %}
  <div>
    {{ content }}
  </div>
{% endsnippet %}

{% render div, content: "Some content" %}
{% render div, content: "Other content" %}
```

```html title="output"
<div>Some content</div>

<div>Other content</div>
```

Inline snippets share the same namespace as variables defined with `{% assign %}` and `{% capture %}`, so be wary of accidentally overwriting snippets with non-snippet data.

```liquid
{% snippet foo %}Hello{% endsnippet %}
{% foo = 42 %}
{% render foo %} {% # error %}
```

Snippets can be nested and follow the same scoping rules as file-based snippets.

```liquid
{% snippet a %}
  b
  {% snippet c %}
    d
  {% endsnippet %}
  {% render c %}
{% endsnippet %}

{% render a %}
{% render c %} {% # error, c is out of scope %}
```

Snippet blocks are bound to their names late. You can conditionally define multiple snippets with the same name and pick one at render time.

```liquid
{% if x %}
  {% snippet a %}b{% endsnippet %}
{% else %}
  {% snippet a %}c{% endsnippet %}
{% endif %}
{% render a %}
```

## component, slot and yield

The `component`, `slot` and `yield` tags are opt-in component tags, enabled with `Environment(extra=True)`. They are similar to the standard `include` and `render` tags, but let a component template decide when and where to render blocks of content provided by the caller. The standard `include` and `render` tags are unaffected.

### component

```plain
{% component <name>[, <argument>: <value>, ...] %}
  [{% slot <name> %} ... {% endslot %}] ...
{% endcomponent %}
```

`name` is a quoted template name or a variable that resolves to one. Like `render`, a component receives only the listed keyword arguments and renders in an isolated scope; assignments made inside the component do not leak back to the caller.

Given this template, saved as `card.liquid`:

```liquid
<div class="card">
  <h1>{% yield title %}</h1>
  <div class="body">{% yield body %}Default body{% endyield %}</div>
  <h1>{% yield title %}</h1>
</div>
```

We render it like this:

```liquid
{% component "card", title: product.name %}
  {% slot body %}<p>{{ product.description }}</p>{% endslot %}
{% endcomponent %}
```

### slot

`{% slot %}` captures a named block at the component's call site. The slot name is a bare word or quoted string. Any content not inside a `slot` tag becomes the unnamed (default) slot. If a slot name is repeated, the last definition wins. `slot` tags never render on their own; they only produce output where the component yields them.

### yield

`{% yield <name>[, <argument>: <value>, ...] %}` renders the slot with the matching name. When no matching slot was provided, it renders nothing, or the default content between `{% yield %}` and `{% endyield %}` if given. Yielding a slot more than once renders the same block each time.

- Slot blocks keep the lexical scope of their call site. They can not see variables or keyword arguments local to the component.
- Keyword arguments passed to `yield` are evaluated in the component's scope and layered on top of the call-site scope while the slot block renders.
- Assignments made inside a slot block are discarded when the slot finishes, so rendering a slot never mutates the caller or a later render of the same slot.
- An unknown `yield` name renders nothing (or its default content).

### unnamed slots and escaping

Yield without a name renders content placed directly in the component body:

```liquid
{% component "card" %}Hello, {{ user.name }}!{% endcomponent %}
```

HTML auto-escaping inside slots follows the same rules as any other template fragment, both when rendering synchronously and asynchronously. Recursive components are limited by the environment's existing context depth limit.
