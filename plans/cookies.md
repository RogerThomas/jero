# Plan: Cookie support (Set-Cookie out, Cookie in)

Status: **partially designed, not built.** The shape is locked; four decisions
(marked **OPEN** below) still need sign-off before building. Captured mid-design
so we can pivot and resume later.

## Goal

First-class cookies, so the developer never hand-formats a `Set-Cookie` string.
Two sides, deliberately **asymmetric** because HTTP is:

- **Response (`Set-Cookie`)** — rich: name, value, and attributes (`Max-Age`,
  `Expires`, `Domain`, `Path`, `Secure`, `HttpOnly`, `SameSite`), one header
  **per cookie** (repeats).
- **Request (`Cookie`)** — flat: `name=value; name=value` in a single header,
  **no attributes**.

We do **not** force a mirror. Response gets a rich `Cookie` type; request is just
another typed Struct source like `params`/`headers`. Forcing symmetry would
invent request-side attributes that don't exist on the wire.

This builds directly on the typed-headers work already landed: response header
assembly funnels through one seam, `_header_items(typed, raw)`, which becomes
`_header_items(typed, cookies, raw)` — emitting **typed headers → Set-Cookie
lines → raw_headers**. No other change to the send path.

## Exported public API

New module `jero/cookies.py` (types + an un-underscored `encode_set_cookie`
boundary-crosser, mirroring `encode_sse` in `streaming.py`; also
`parse_cookie_header` for the request side). `Cookie` is a `@dataclass(slots=True)`
— it matches the `ServerSentEvent` precedent (user-constructed, serialized to a
header string, never JSON-decoded).

```python
@dataclass(slots=True)
class Cookie:
    name: str
    value: str
    max_age: int | None = None
    expires: datetime | None = None        # -> IMF-fixdate (RFC 7231)
    domain: str | None = None
    path: str = "/"                         # OPEN #3
    secure: bool = False                    # OPEN #1 (default)
    http_only: bool = True                  # OPEN #1 (default)
    same_site: Literal["strict", "lax", "none"] | None = "lax"   # OPEN #1 (default)
    partitioned: bool = False               # CHIPS

    @classmethod
    def expired(cls, name: str, *, path: str = "/", domain: str | None = None) -> "Cookie":
        """A cookie that deletes its namesake (value='', max_age=0). OPEN #4."""
        ...
```

Exported from `jero`: `Cookie`. Lives in `jero/cookies.py`.

## Response side — setting cookies

A new field on `BaseResponse` (core) and `_StreamingResponse` (streaming.py):

```python
cookies: Sequence[Cookie] | None = None
```

A list → one `Set-Cookie` per item (repeats handled naturally, sidestepping the
single-value-per-field limit a Struct would impose). Plugs into the existing
`_header_items` seam; framework still manages `content-type`/`content-length`.
Bare `Struct`/`bytes` returns can't set cookies (no wrapper) — same limitation as
typed headers, and fine.

`encode_set_cookie(cookie: Cookie) -> str` builds the header value:
`name=value; Max-Age=...; Path=/; Secure; HttpOnly; SameSite=Lax`, omitting
unset attributes, formatting `expires` as IMF-fixdate.

## Request side — reading cookies

A new `cookies` handler-arg source (add `"cookies"` to `_SOURCES`): a typed
Struct, parsed from the `Cookie` header into `{name: value}` then `convert`-ed
exactly like `params`/`headers` (string → typed scalar coercion).

Wrinkle vs headers: cookie names are **case-sensitive and arbitrary**, so match
by field name **exactly** — **no dash↔underscore mangle** — with msgspec
`rename` for names that aren't valid identifiers (`sid-token`). This is a
deliberate difference from header binding (which mangles). (**OPEN #4** — confirm.)

## OPEN decisions (need sign-off before building)

1. **Security defaults (the big one).** Recommendation: `http_only=True`,
   `same_site="lax"`, `secure=False` — secure where it's free (those two), but
   not forcing HTTPS so local plain-HTTP dev works; opt into `secure=True`.
   Alternative A: fully secure-by-default (`secure=True` too) — breaks local dev.
   Alternative B: permissive/all-off like FastAPI/Starlette — friendlier, ships
   insecure defaults.
2. **Value encoding.** Recommendation: **validate the value against the legal
   RFC 6265 cookie-octet set and raise on illegal chars** at emit time —
   predictable, matches the framework's fail-loud stance, user controls any
   encoding. Alternatives: auto percent-encode (silently transforms), or lean on
   stdlib `http.cookies` (inherits its quirky double-quote escaping — rejected).
3. **`path` default.** Recommendation: default `path="/"` (the near-universal
   intent; avoids surprise path-scoping). Alternative: `None` (don't emit).
4. **Request-side name mapping.** Recommendation: exact field-name match +
   `rename` for non-identifiers, **no mangle**, case-sensitive. Confirm.

## Staged build order (once decisions locked)

1. `jero/cookies.py`: `Cookie` dataclass + `encode_set_cookie` + `parse_cookie_header`
   (pure; the SSE/NDJSON-style thin test layer applies here too once stable).
2. Response: add `cookies` to `BaseResponse` + `_StreamingResponse`; extend
   `_header_items` to `_header_items(typed, cookies, raw)`; thread through every
   sender call site.
3. Request: add `"cookies"` to `_SOURCES`; resolve as a Struct in `_bind_sources`;
   parse + `convert` in `_Binder`.
4. Export `Cookie` from `jero/__init__.py`.
5. Tests through `TestClient`: set one/many cookies (repeats), attribute
   serialization, `expired`, illegal-value rejection, read cookies into a Struct,
   `rename` for non-identifier names, cookies + typed headers + raw_headers together.

## Notes / interactions

- A `Set-Cookie` set via `raw_headers` AND via `cookies` both emit (append) — fine.
- OpenAPI: the typed `Cookie`/request `cookies` Struct is what the future spec
  derives cookie params/headers from — same rationale as typed headers.
- Future (out of scope): signed/encrypted cookies, session middleware.
