# Plan: Fail loud on a malformed `openapi_security` declaration

Status: **built (2026-07-29).** Small and local — one guard, four tests, two doc lines. No
API change for any correct app; breaking only for apps whose spec is already wrong.

## Goal

An authenticator declares its OpenAPI scheme with a class attribute, read at wiring
(`jero/core.py:2757`, in `_include`):

```python
declared = getattr(type(auth), "openapi_security", None)
security_scheme = declared if isinstance(declared, SecurityScheme) else SecurityScheme.http_bearer()
```

A declaration that is *present but not a* `SecurityScheme` silently falls through to the
bearer default. So both of these boot happily and publish a spec claiming HTTP bearer:

```python
class CookieAuth:
    openapi_security = "apiKey"                                   # wrong type
    ...

class QueryAuth:
    openapi_secuirty = SecurityScheme.api_key(name="k", location="query")   # typo'd name
    ...
```

The result is a spec that lies about how to authenticate, with nothing at startup to say so —
the failure mode jero otherwise refuses. Every other "declared but wrong type" case already
fails loud: `_check_meta_types` (`jero/core.py:300`) raises a `WiringError` when a shape is
handed the wrong meta object. This closes the one gap in that rule.

## Decisions

DECIDED: **present but not a `SecurityScheme` → `WiringError` at wiring.** Message names the
authenticator, since that is where the mistake is, matching the meta check's phrasing:
`f"{type(auth).__name__}: openapi_security must be SecurityScheme, got {type(declared).__name__}"`.

DECIDED: **absent stays legal and keeps defaulting to bearer.** A structural authenticator that
declares nothing is a supported, tested case (`tests/test_openapi.py:328`) and the bearer
default is the documented common case.

DECIDED: **explicit `None` counts as absent, not as an error.** An implementor may reasonably
annotate `openapi_security: ClassVar[SecurityScheme | None] = None`; that must keep working, so
the guard triggers on `declared is not None and not isinstance(declared, SecurityScheme)`.

DECIDED: **the guard sits beside the `_CompiledAuth(auth)` construction in `_include`.** It is
the point where the auth object is already being inspected; no new traversal, no new call site.

REJECTED: **catching the typo'd attribute name.** An absent attribute is legal, so a misspelling
is indistinguishable from "declares nothing" without guessing at near-miss names. Out of scope,
and the wrong-type half is the one that produces a confidently wrong spec.

REJECTED: **moving the declaration onto a meta object** (`AuthMeta`, or an `auth_meta=` class
keyword). `Auth` is a `Protocol` — an authenticator need not inherit any jero base, so there is
no `__init_subclass__` to intercept a class keyword. The `ClassVar` is the only channel that
works for a duck-typed shape; `BearerAuth` / `BasicAuth` already cover the inherit-instead
preference.

REJECTED: **declaring the scheme at the mount site** (`include_endpoint(..., security=...)`). The
scheme is a property of the authenticator, not of each route that mounts it — repeating it per
mount invites contradictory declarations.

## Tests

`tests/test_openapi.py`, alongside the existing security-scheme cases:

1. An authenticator with `openapi_security = "apiKey"` fails app startup with a `WiringError`
   naming the class and the offending type.
2. `openapi_security = None` still boots and yields the bearer requirement (pins the decision
   above).
3. Unchanged: a bare structural authenticator defaults to bearer; a valid
   `SecurityScheme.api_key(...)` is honoured; `BearerAuth` / `BasicAuth` are honoured.

## Docs

- `docs/guide/auth.md` and `docs/guide/openapi.md` both describe the attribute — add that it
  must be a `SecurityScheme` (or absent) and that anything else is a startup error.

## Staged build order

Single stage; ends green on `uv run prek run -a`, `task test PYTEST_PROFILE=agent`,
`task typecheck-tests`.

1. Add the guard in `_include`, add the four tests, add the two doc sentences.
