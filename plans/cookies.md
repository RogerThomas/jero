# Plan: Native cookies (typed binding, SetCookie responses, cookie auth)

Status: **built, all stages green.** Shipped in #45 ("Inbuilt cookies"). All
decisions below were locked (scope and security posture confirmed with the
project owner on 2026-08-12) and built in the staged order at the end.

## Read this first (context for the implementing agent)

- Read `style-guide.md` and `AGENTS.md` before writing any code. The rules that
  bite most here: `@dataclass` for behavior-carrying classes, `Struct` for pure
  DTOs, private helpers *before* their callers, no nested functions, no
  module-level data globals, never suppress a lint/type error without asking,
  tests only through the public boundary (`TestClient` + `demo_app/` or small
  local apps), and simple kebab-case test values.
- Verify with `task check` (`PYTEST_PROFILE=agent` for test tasks). Type gates:
  `jero/` is checked by pyrefly; `tests/` + `demo_app/` must additionally stay
  clean under mypy/ty/pyright/zuban. `prek run -a` only lints *tracked* files,
  so run the checkers directly on any new file before relying on it.
- Never use `from __future__ import annotations`. jero targets 3.13+ with eager
  annotations.
- Do not commit anything unless explicitly asked.
- Key files: `jero/core.py` (binding, `_Binder`, `_CompiledAuth`, response
  wrappers, senders), `jero/_wiring_types.py` (`Sources`, `OperationSpec`),
  `jero/_openapi_wiring.py` (`_params_for`, `_response_entry` inputs),
  `jero/openapi.py` (`ParamSpec`, `Location`, `_parameters`,
  `SecurityScheme.api_key`), `jero/_exception_handlers.py`
  (`ExceptionResponse`), `jero/streaming.py` (`StreamingResponse` headers),
  `jero/testing.py` (`TestClient`, `TestResponse`), `jero/headers.py`
  (`RawHeaders`, the closest existing analog to the new module).

## Goal

Cookies as a first-class, typed, compiled citizen on both sides of the wire:

- **Reading**: a `cookies` binding source — a msgspec Struct bound by argument
  name, exactly like `headers`, on HTTP handlers *and* WebSocket handshakes,
  documented as `in: cookie` OpenAPI parameters.
- **Writing**: a validated `SetCookie` value on every response wrapper
  (buffered, streaming, `ExceptionResponse`), secure by default, replacing the
  current `raw_headers={"Set-Cookie": ...}` escape hatch as the blessed way.
- **Auth**: an authenticator may declare `cookies` (instead of, or alongside,
  `headers`), making browser session-cookie auth and cookie-authenticated
  WebSockets first-class.

Everything resolves at wiring time; the per-request cost is one small header
parse on routes that declared cookies and zero elsewhere. Signed-cookie
sessions are **out of scope** (a possible later plan layered on this one).

The new public surface is four names: `SetCookie`, `CookieAuth`, `HybridAuth`,
and the `cookies` argument-name vocabulary (plus `TestCookie` on the testing
side).

## Public API

```python
from typing import Literal
from msgspec import field

from jero import Endpoint, JSONResponse, NoContent, SetCookie, Struct


class SessionCookies(Struct):
    # Field name == cookie name, verbatim (cookies are case-sensitive; there
    # is NO snake_case mangle, unlike headers). Non-identifier names use
    # msgspec's per-field rename:
    session_id: str
    theme: str = "dark"                                   # optional, with default
    host_token: str | None = field(default=None, name="__Host-token")


class SessionEndpoint(Endpoint, path="/session"):
    async def post(self, json: LoginRequest) -> NoContent:
        token = ...
        return NoContent(cookies=[SetCookie("session_id", token)])
        # ^ secure defaults: Path=/; HttpOnly; Secure; SameSite=Lax

    async def get(self, cookies: SessionCookies) -> Profile: ...

    async def delete(self) -> NoContent:
        return NoContent(cookies=[SetCookie.expire("session_id")])
```

Cookie-based auth (the return-type-is-the-policy contract is unchanged):

```python
@dataclass
class SessionAuth:  # satisfies the CookieAuth protocol structurally
    _user_service: UserService

    async def authenticate(self, cookies: SessionCookies) -> User:
        return await self._user_service.by_session(cookies.session_id)
```

## The `cookies` binding source

DECIDED — semantics mirror `headers` with cookie-specific rules:

- New allowed argument name `cookies` on every HTTP handler and on WebSocket
  `handle` handshakes. Annotated as a msgspec Struct (via the existing
  `_struct_annotation` path); anything else is a `WiringError`. Allowed on
  bodyless verbs (a GET reading cookies is the normal case).
- **Names bind verbatim and case-sensitively.** RFC 6265 cookie names are
  case-sensitive and include non-identifier shapes (`__Host-session`,
  `csrftoken`), so there is no header-style mangle. A non-identifier name is
  declared with `msgspec.field(name="__Host-session")` (or a Struct-level
  `rename`); msgspec's `convert` honors it.
- **Parsing is lenient; binding is strict.** Browsers send *every* cookie
  scoped to the domain/path, including other applications' garbage. A
  malformed *fragment* (`;;`, a nameless `=x`, a valueless bare token) is
  skipped, never a 400 — rejecting a request because an unrelated app on the
  same domain set a broken cookie is unacceptable. Unknown cookie names are
  ignored (default msgspec behavior). But *your* declared fields validate
  strictly: a missing required cookie or a value failing conversion is a 400
  (`MalformedRequestError`), exactly like `headers` — via
  `_convert_source(cookie_dict, cookies_type, 400)`.
- Parse rules: split the Cookie header on `;`, each fragment on the *first*
  `=`; strip optional whitespace; strip one pair of surrounding double quotes
  from a value (RFC 6265 allows DQUOTE-wrapped values). **First occurrence of
  a duplicated name wins.** No percent-decoding or other unescaping — RFC 6265
  defines none, and jero does not guess at app-level encoding conventions; the
  value binds verbatim (scalar conversion, e.g. `str` -> `int`, still applies
  through `convert(..., strict=False)`, as for query params).
- **Multiple `Cookie` headers must be joined with `"; "` before parsing.**
  HTTP/2 (RFC 9113 §8.2.3) permits clients to split the cookie header into
  several field lines; granian will surface them as repeated entries in
  `scope["headers"]`. Collect all of them, not just the first.
- Hot-path discipline: routes that declare no cookies anywhere (source or
  auth) pay nothing. Compile a `_needs_cookies` flag on `_Binder` (analog of
  `_needs_raw`); when only cookies are needed, scan `scope["headers"]` for
  `b"cookie"` entries directly rather than building the full `_raw_headers`
  dict. Parse once per request and share the resulting `dict[str, str]`
  between auth and the handler source.

## `SetCookie` — the response side

DECIDED — a frozen, slotted dataclass in a new leaf module `jero/cookies.py`
(the `jero/headers.py` precedent: response-side vocabulary, never a msgspec
wire model). Positional `name`, `value`; everything else keyword-only
(`dataclasses.KW_ONLY`):

```python
@dataclass(frozen=True, slots=True)
class SetCookie:
    name: str
    value: str = ""
    _: KW_ONLY
    max_age: int | None = None
    expires: datetime | None = None          # must be timezone-aware
    path: str | None = "/"
    domain: str | None = None
    secure: bool = True
    http_only: bool = True
    same_site: Literal["strict", "lax", "none"] | None = "lax"
    partitioned: bool = False
```

DECIDED — **secure by default** (confirmed with the owner): a bare
`SetCookie("session", token)` is already `Path=/; Secure; HttpOnly;
SameSite=Lax`. Loosening (`http_only=False` for a JS-readable cookie,
`secure=False`) is explicit and visible in review. Modern browsers treat
`http://localhost` as a trustworthy origin, so Secure cookies work in local
dev; the docs say this so nobody "fixes" it.

DECIDED — validation happens in `__post_init__`, raising `ValueError` at
construction (fail loud at the raise site, jero style):

- `name`: non-empty RFC 6265 token — no control chars or separators
  (`()<>@,;:\"/[]?={}`, space, tab).
- `value`: cookie-octets only (printable ASCII minus space, DQUOTE, comma,
  semicolon, backslash). Empty allowed. jero emits the value bare, never
  quoted.
- `max_age`: an `int` and not a `bool` (the existing `isinstance(x, bool)`
  guard idiom, see `ExceptionResponse.__post_init__`).
- `expires`: timezone-aware or rejected; encoded as IMF-fixdate in GMT
  (`Wdy, DD Mon YYYY HH:MM:SS GMT`).
- `path`: if given, must start with `/`. `domain`: `None` or non-empty.
- `same_site="none"` requires `secure=True` (browsers already enforce this;
  jero makes it unrepresentable instead of silently dropped).
- `partitioned=True` requires `secure=True` (CHIPS rule).
- `__Host-` prefix requires `secure=True`, `path == "/"`, `domain is None`;
  `__Secure-` prefix requires `secure=True`.

DECIDED — deletion is a named constructor, not a kwargs recipe:
`SetCookie.expire(name, *, path="/", domain=None)` returns a `SetCookie` with
empty value, `max_age=0`, **and** `expires` at the Unix epoch (belt and braces
for clients that ignore Max-Age). Path/domain must match the original cookie
for the browser to remove it — the docstring says so.

DECIDED — wire encoding is a module-level boundary-crosser
`encode_set_cookie(cookie: SetCookie) -> str` (the `encode_sse` idiom:
un-underscored, imported by `core`, not exported from `jero`). Attribute
order: `name=value; Max-Age=…; Expires=…; Domain=…; Path=…; Secure; HttpOnly;
SameSite=…; Partitioned`, omitting `None`/`False` attributes.
`parse_cookie_header(value: str) -> dict[str, str]` (the request-side parser
described above) lives in the same module. Style rule 5: both helpers and any
private character-class tables go *above* their callers; the character sets
live as class attributes or module functions per the globals rule (a frozen
`frozenset` constant used as a lookup table is the "infrastructure" shape —
keep it minimal and private).

DECIDED — every response wrapper gains `cookies: Sequence[SetCookie] = ()`,
next to `links`:

- `BaseResponse` in `core.py` (so `JSONResponse` / `BytesResponse` /
  `NoContent` / `Created` / `Accepted` all carry it),
- `StreamingResponse` in `streaming.py` (all streaming kinds),
- `ExceptionResponse` in `_exception_handlers.py` (an auth failure that
  expires a stale session cookie is a real case).

Emission threads through the existing `_header_items(typed, raw)` seam in
`core.py`: **typed headers first, then one `set-cookie` pair per entry, then
`raw_headers` last** (the escape hatch stays last so its own repeats survive,
and a hand-rolled raw `Set-Cookie` keeps working during migration). All
senders — buffered, no-content, streaming — flow through that seam; extend its
signature rather than duplicating the loop. Two entries with the same `name`
in one response are a programming error: raise `ValueError` at emission (it
surfaces as the logged generic 500; N is tiny, a set-based check is fine).

Middleware's `response_headers` tail appends after all of this and is
unaffected. `_no_content_headers`' 204 content-type/length stripping does not
touch `set-cookie`.

## Cookie auth

DECIDED — `authenticate` may declare any non-empty subset of
`{headers, cookies}`, each a Struct:

- `authenticate(headers: H)` — unchanged, existing apps untouched.
- `authenticate(cookies: C)` — session-cookie auth.
- `authenticate(headers: H, cookies: C)` — hybrid (one app serving bearer API
  clients and cookie browser clients on the same routes).

`_CompiledAuth` (core.py) grows a `cookies_type` beside `headers_type`; the
signature check (currently "exactly one argument named `headers`", line
~1394) becomes "one or two arguments, each named `headers` or `cookies`", with
a precise `WiringError` otherwise. Binding failure on either source maps to
401 (`AuthenticationRequiredError`), exactly as headers do today. The
return-type-is-the-policy contract is source-independent and unchanged: for
absence to *reach* `authenticate` (the `-> TUser | None` optional-auth form),
the cookie field needs a `| None = None` default, same as the header rule —
extend the `Auth` docstring's absence paragraph to say "field", covering both.

DECIDED — static typing: `Auth[THeaders, TUser]` keeps its exact shape (no
break for existing implementers). Two new protocols beside it in `core.py`:

- `CookieAuth[TCookies: Struct, TUser: Struct]` —
  `authenticate(self, cookies: TCookies) -> TUser | Awaitable[TUser | None] | None`
- `HybridAuth[THeaders: Struct, TCookies: Struct, TUser: Struct]` —
  `authenticate(self, headers: THeaders, cookies: TCookies) -> ...`

Both are exported. The mount signatures (`_include_resource`,
`_include_endpoint`, `_include_websocket`) widen their `auth` parameter to the
three-way union with a `TCookies: Struct` type parameter added. Wiring stays
structural (the compiled introspection is the truth; the protocols are the
static face). `BearerAuth`/`BasicAuth` sugar classes are untouched.

DECIDED — OpenAPI security derivation (the block at core.py ~3421):

- headers-only authenticator, nothing declared → `http_bearer()` (unchanged).
- cookies-only authenticator whose cookies Struct has **exactly one field**,
  nothing declared → derive `SecurityScheme.api_key(location="cookie",
  name=<that field's wire name>)`.
- cookies-only with several fields, or hybrid, nothing declared → store
  `security_scheme=None` at include time and **fail at `_include_openapi`**
  ("declare `openapi_security` on `<AuthClass>`") — the established home for
  document-only checks (streaming item-type precedent), so apps that never
  wire OpenAPI never pay. Detection: `auth_mode is not None and
  security_scheme is None` on the `OperationSpec`.
- an explicit `openapi_security` always wins, as today.

## OpenAPI

- `Location` literal in `openapi.py` gains `"cookie"`; `_params_for` in
  `_openapi_wiring.py` appends `ParamSpec("cookie", sources.cookies)`.
- `_parameters` in `openapi.py`: cookie params use `field_info.encode_name`
  **verbatim** (rename respected, no header-style mangle) — extend the
  docstring, which currently explains the header inversion.
- The derived-400 condition in `_openapi_wiring.py` (~line 381: "binding can
  fail") extends to `sources.cookies is not None`.
- DECIDED — **response `Set-Cookie` is not modeled in the spec.** Two reasons,
  stated in the docs: OpenAPI itself cannot describe multiple `Set-Cookie`
  headers, and response cookies are per-instance runtime values, not part of
  the static return type. This is the WebSockets precedent (a stated boundary,
  not a gap). A user who insists can still declare a single documented
  `set_cookie` field on a typed `H` headers Struct.

## WebSockets

`cookies` joins the allowed handshake sources (the frozenset at core.py ~1345
and `_bind_websocket_sources`), binding before implicit accept exactly like
`headers`. Cookie auth on the handshake is the *motivating* browser case (the
browser WebSocket API cannot set an Authorization header; it always sends
cookies). Rejections flow through the existing denial-response path
unchanged. Update the `WebSocketEndpoint` docstring's source vocabulary line.

## Middleware — deliberately out of scope

- `Request[H]` gets no cookie binding in v1. A middleware that genuinely needs
  the raw cookie can bind `cookie: str | None = None` in its `H` Struct today;
  say so in the middleware guide.
- Middleware cannot emit `Set-Cookie` (the `response_headers` machinery models
  constant pairs and single-valued Structs, not repeats). A session-refresh
  middleware is real future work; note it under deferred, don't wedge it in.

## Testing

`TestClient` (jero/testing.py) grows the typed harness half:

- Every request method (and `websocket(...)`) accepts
  `cookies: Mapping[str, str] | None = None`, encoded as one `Cookie` header
  (`"; ".join(f"{name}={value}")`). Passing `cookies=` *and* an explicit
  `Cookie` entry in `headers=` is ambiguous → `ValueError`.
- `TestResponse.cookies` property → `dict[str, TestCookie]` parsed from the
  `set-cookie` entries in `multi_headers` (which already preserves repeats).
  `TestCookie` is a small frozen dataclass: `value`, `max_age: int | None`,
  `expires: str | None` (raw, no date parsing), `path`, `domain`,
  `secure: bool`, `http_only: bool`, `same_site: str | None`,
  `partitioned: bool`. Attribute names parse case-insensitively.
- DECIDED — **an opt-in cookie jar, off by default** (the reqwest shape, not
  httpx's always-on): `TestClient(app, cookie_jar=True)`. Off (the default),
  every request sends only what you explicitly pass — nothing hidden, no state
  leaking between test steps. On, the client stores each response's
  `Set-Cookie` values in `client.cookie_jar` (a plain, directly inspectable
  and mutable `dict[str, str]`) and attaches them to subsequent requests and
  WebSocket handshakes; an expiring `Set-Cookie` (`Max-Age=0` or a
  past `Expires`) removes its entry, so a logout test can assert the jar
  emptied. Per-request `cookies=` merges *over* the jar (explicit wins on
  name collisions). The jar is name → value only — no path/domain scoping,
  deliberately: the harness is single-origin and in-process, so RFC 6265
  scoping rules would be dead code (say so in its docstring).

Test coverage (through the public boundary only — `TestClient` against
`demo_app/` plus small local apps for focused cases, per the project's
testing stance; follow the scaffolding shape of `tests/test_headers.py` and
`tests/test_auth.py`):

- binding: required/optional/defaulted fields; scalar conversion (`int`);
  verbatim case-sensitive names; `msgspec.field(name="__Host-x")` rename;
  unknown cookies ignored; duplicate name → first wins; quoted value
  unwrapped; malformed fragments skipped while good ones bind; missing
  required cookie → 400 problem body; split Cookie headers (two entries in
  the request) both bind.
- wiring failures: `cookies` annotated as non-Struct; authenticate with a
  wrong param name; authenticate with zero params.
- auth: cookie auth 401 on missing/invalid; optional cookie auth
  (`-> User | None` + `| None = None` field) serves anonymous; hybrid
  authenticator binds both sources; `user` cross-checks unchanged.
- responses: SetCookie on every wrapper kind (JSON, NoContent 204, Created,
  streaming, `ExceptionResponse`); secure defaults on the wire; attribute
  formatting incl. `Expires` IMF-fixdate and `Partitioned`; `expire()`
  emits Max-Age=0 + epoch Expires; construction `ValueError`s (prefix rules,
  samesite=none w/o secure, naive datetime, bad name chars); duplicate names
  in one response → 500; `raw_headers` Set-Cookie still passes through after
  typed cookies.
- websockets: handshake cookie binding; cookie-auth accept and pre-upgrade
  rejection.
- openapi: `in: cookie` params with verbatim names and required/optional;
  derived apiKey-cookie scheme from a single-field cookies-only auth; the
  underivable-scheme `WiringError` under `_include_openapi`; absence of any
  Set-Cookie response modeling.
- testing surface: `cookies=` param round-trip; `TestResponse.cookies`
  attribute parsing; the ambiguous-cookie `ValueError`; the jar — off by
  default (no persistence between requests), persistence across requests and
  into WebSocket handshakes when enabled, expiring `Set-Cookie` removes the
  entry, per-request `cookies=` overriding a jar entry, direct
  `client.cookie_jar` mutation.

`demo_app/` gains the living example, kept bounded: a `SessionAuth`
(cookie authenticator over the existing user lookup in `auth.py`), a
`SessionEndpoint` at `/session` (`post` = login sets the cookie, `delete` =
expires it), and one small cookie-gated read (e.g. `/profile`) mounted with
`SessionAuth` — enough to exercise binding, SetCookie, expire, and cookie auth
end to end without turning demo_app into a dumping ground.

## Docs

- New `docs/guide/cookies.md`: reading cookies (verbatim names + the rename
  idiom), why parsing is lenient but binding is strict, setting cookies
  (secure defaults, and *why* — with the localhost note), deleting, cookie
  auth incl. hybrid and the WebSocket browser story, the OpenAPI boundary
  (request params documented; Set-Cookie responses not representable),
  testing idiom. **Every example must be a complete, wired, runnable app**
  (project rule) — model them on the existing guide pages.
- Touch: `binding.md` (source table + the no-mangle contrast with headers),
  `auth.md` (cookie/hybrid authenticators, absence-needs-a-default rule),
  `responses.md` (`cookies=` field, raw_headers demoted from "e.g. Set-Cookie"
  to exotic-names-only), `websockets.md` (handshake source list),
  `testing.md`, `migration-from-fastapi.md` (FastAPI `Cookie()` /
  `response.set_cookie` mapping table entry).
- `jero/__init__.py`: export `SetCookie`, `CookieAuth`, `HybridAuth` (and
  `TestCookie` stays in `jero.testing`). Keep `__all__` sorted.
- Update `AGENTS.md` in the same change: the contract bullet list (binding
  sources, auth paragraph, response wrappers), Layout (`jero/cookies.py`),
  Status (built list), and flip this plan's row in the plans note.

## Performance notes

Routes not touching cookies: zero new work (the flag compiles to the existing
early-outs). Routes that do: one linear scan of `scope["headers"]` for
`b"cookie"`, one small string parse, one `convert` — the same shape as the
headers source. Response side: `encode_set_cookie` is a handful of appends on
an already-built response; no per-request introspection anywhere (the
character-class validation runs at `SetCookie` construction, which for
long-lived values like expiry sentinels can be hoisted by the user). Run
`bench.py` before/after on the existing scenarios to confirm the untouched
hot path is unmoved.

## Deferred (explicitly not v1)

1. Signed/encrypted cookie sessions (HMAC session primitive) — separate plan.
2. Middleware-emitted `Set-Cookie` (session refresh) — needs a repeats-capable
   middleware header tier.
3. `Request[H]` typed cookie binding for middleware.
4. Per-field cookie documentation of responses, should OpenAPI ever grow a
   real Set-Cookie model.

## Staged build order

1. **`jero/cookies.py`**: `SetCookie` + validation + `expire`,
   `encode_set_cookie`, `parse_cookie_header`.
2. **Request binding**: `Sources.cookies`, `_SOURCES`, `_bind_sources`,
   `_Binder` (incl. the split-header join and `_needs_cookies` fast path),
   WebSocket handshake sources; `TestClient` `cookies=` param (needed to test
   anything); binding + wiring-failure tests.
3. **Response side**: `cookies` on `BaseResponse` / `StreamingResponse` /
   `ExceptionResponse`, the `_header_items` seam + duplicate check,
   `TestResponse.cookies` / `TestCookie`, the opt-in `cookie_jar` (it needs
   the Set-Cookie parsing this stage builds); response + jar tests.
4. **Auth**: `_CompiledAuth` widening, `CookieAuth` / `HybridAuth` protocols,
   mount-signature unions, security-scheme derivation + the
   `_include_openapi`-time check; auth + websocket-auth tests; `demo_app`
   session surface.
5. **OpenAPI**: `Location` "cookie", `_params_for`, `_parameters` verbatim
   naming, derived-400 condition; spec tests.
6. **Docs + exports**: `docs/guide/cookies.md`, guide touch-ups,
   `__init__.py`, `AGENTS.md`; `bench.py` before/after sanity run.
