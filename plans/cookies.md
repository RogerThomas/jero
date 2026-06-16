# Plan: Cookie support (Set-Cookie out, Cookie in)

Status: **designed, not built.** Shape and all four decisions are locked (see
the DECIDED notes below). Build in the staged order at the bottom.

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
    path: str = "/"                         # DECIDED #3
    secure: bool = False                    # DECIDED #1
    http_only: bool = True                  # DECIDED #1
    same_site: Literal["strict", "lax", "none"] | None = "lax"   # DECIDED #1
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
deliberate difference from header binding (which mangles). (**DECIDED #4**.)

## OPEN decisions (need sign-off before building)

1. **Security defaults — DECIDED:** `http_only=True`, `same_site="lax"`,
   `secure=False`. Secure where it's free; not forcing HTTPS so local plain-HTTP
   dev works. Every default is overridable per-cookie at construction
   (`Cookie("sid", "x", secure=True, http_only=False, same_site="none")`) — the
   defaults only decide what you get when you don't say.
2. **Value encoding — DECIDED:** validate the value against the legal RFC 6265
   cookie-octet set and **raise on illegal chars at emit time**. Predictable,
   fail-loud, user controls any encoding. (Rejected: auto percent-encode — silently
   transforms; stdlib `http.cookies` — quirky double-quote escaping.)
3. **`path` default — DECIDED:** `path="/"` (near-universal intent; avoids
   surprise path-scoping). Overridable per-cookie like any field.
4. **Request-side name mapping — DECIDED:** exact field-name match + `rename`
   for non-identifiers, **no mangle**, case-sensitive.

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
