# Project structure

The [complete example](complete-example.md) is one file so the whole shape is visible
at a glance. A real app splits the same idea into modules — and jero ships one:
[`demo_app/`](https://github.com/RogerThomas/jero/tree/main/demo_app), the
project-structured widgets API the [test suite runs against](testing-approach.md), so
it is always working and always idiomatic. This page walks its layout.

## The layout

```
demo_app/
├── app.py           # the BaseApp subclass — wire() is the whole wiring story
├── factory.py       # the composition root: builds services on the app's exit stacks
├── config.py        # environment-selected settings
├── models.py        # the Struct contracts: wire models, params, headers, the auth user
├── auth.py          # authenticators (gating + optional, over one shared lookup)
├── errors.py        # error contracts + the custom exception handler
├── services/        # the I/O layer: one class per capability
│   ├── widget_service.py
│   ├── analytics_service.py
│   └── question_service.py
└── operations/      # the routes: Resources & Endpoints grouped by feature
    ├── widget_operations.py
    ├── system_operations.py
    └── streaming_operations.py
```

| Module | What lives there | Guide |
| --- | --- | --- |
| `app.py` | The `BaseApp[Factory]` subclass. `wire` builds services from the factory, opens the background queue, and lists every include — the app's whole topology on one screen. | [Wiring](wiring.md) |
| `factory.py` | The `BaseFactory`. Each `create_*` builds one service, entering anything with lifecycle via `_aenter`. Tests swap the whole thing through the `factory=` seam. | [Wiring](wiring.md#factories), [Testing](testing.md) |
| `config.py` | Environment-specific settings. jero ships no settings system; the demo's convention is pydantic-settings for env *parsing*, mapped into a msgspec `Settings` Struct the services receive. | — |
| `models.py` | Every `Struct` that crosses the wire, inheriting one `Camel` base that fixes the wire convention. | [Binding](binding.md), [OpenAPI](openapi.md#defining-models) |
| `auth.py` | Two authenticators over one token lookup — one gates, one serves anonymous callers — so a route's policy is visible in what its mount passes. | [Authentication](auth.md) |
| `errors.py` | Typed `HTTPError` contracts and the custom exception handler. | [Errors](errors.md) |
| `services/` | Classes that do I/O. They know nothing about HTTP — they take and return models. | — |
| `operations/` | The `Resource` / `Endpoint` classes, one module per feature area. They know nothing about construction — dependencies arrive through `__init__`. | [Resources & Endpoints](resources.md) |

## The dependency direction

Dependencies point one way:

```
operations  →  services  →  config
     ↑             ↑
   app.py  →  factory.py
```

`operations/` call `services/`; `factory.py` constructs services; `app.py` asks the
factory for them and passes them to operations. Nothing imports `app.py`, and no module
reaches sideways for a global — which is why the
[`factory=` test seam](testing.md#mocking-dependencies) can replace the entire I/O
layer without touching auth, routes, or models.

## Adapting it

The names aren't framework contracts — jero only sees what `wire` includes. What's
worth keeping as you rename things:

- **One module owns construction** (the factory), so `wire` stays a short list of
  includes.
- **Routes take dependencies through `__init__`**, never from module scope.
- **Contracts live together** (`models.py`), inheriting one wire-convention base.
- **Pure, lifecycle-free wiring** (like the demo's in-memory token map) can live
  directly in `wire`; the factory is for services that open things.
