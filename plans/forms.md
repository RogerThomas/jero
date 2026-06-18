# Plan: Multipart form binding (`form` source)

Status: **built.** Captures the locked design from the forms
design discussion. Build in the staged order at the bottom. Scope is
**multipart-only, whole-body-buffered, v1** — see Deferred for what's out.

## Goal

A new request body source, `form`, bound by name like `json` / `content`, into a
Struct whose fields are the form's *parts*. Each field is typed end-to-end (the
future OpenAPI work reads it: a file part → `format: binary`, a JSON part → its
nested schema, etc.). Unlike `json`, the form Struct is **not** built by
`msgspec.convert` — the framework parses the multipart body, then constructs each
field according to its compiled kind.

## Exported public API

```python
# jero/forms.py, re-exported from jero

class FormPart[T](Struct):
    """One form part, with its envelope metadata. `T` is the payload type and
    drives how the part body is decoded (see "payload kinds")."""
    data: T
    content_type: str | None
    headers: dict[str, str]          # raw per-part headers (deliberate escape hatch)

class FilePart(FormPart[bytes]):
    """A file upload: FormPart[bytes] plus a required filename."""
    filename: str
```

Exported from `jero`: `FormPart`, `FilePart`. No private base — `FormPart` *is*
the base, and is itself a usable type (`FormPart[JobType]`, `FormPart[bytes]`,
`FormPart[SomeStruct]`); `FilePart` is its one specialization. (Contrast
`BaseResponse`, which is abstract and unexported — here the base is concrete.)

## How an app author uses it

A field is either **bare** (payload only, no envelope) or **wrapped** in
`FormPart[T]` (payload + `content_type` + `headers`). This mirrors the response
family exactly: return a bare `Struct` for the simple case, or `JSONResponse` when
you need to control headers. Same here — bare value, or `FormPart[T]` when you
need the envelope.

```python
from typing import Literal
from msgspec import Struct
from jero import Resource, FormPart, FilePart

type JobType = Literal["export-text", "export-images"]

class JobConfig(Struct):
    dpi: int = 150

class CreateJob(Struct):
    job_type: JobType                 # bare scalar  -> text part, coerced
    document: FilePart                # file (required filename)
    attachments: list[FilePart]       # repeated part, same name
    options: FormPart[JobConfig]      # JSON Struct + content_type/headers
    raw: bytes | None = None          # bare raw-bytes part, optional
    note: FormPart[str] | None = None # text + envelope, optional

class JobsResource(Resource):
    def __init__(self, jobs: JobService) -> None:
        self._jobs = jobs

    async def create(self, form: CreateJob) -> JobAccepted:
        opts = form.options.data                 # JobConfig, decoded + validated
        ct = form.document.content_type          # str | None
        return await self._jobs.enqueue(
            kind=form.job_type,
            document=form.document.data,         # bytes
            filename=form.document.filename,     # str
            extras=[a.data for a in form.attachments],
            options=opts,
        )
```

### Payload kinds (how `T` / a bare type decodes)

The payload type is classified **once at wiring**. The three cases are disjoint,
so dispatch is unambiguous (same disjointness the bare-field and streaming-source
logic already rely on):

| payload type      | decode                                   |
| ----------------- | ---------------------------------------- |
| `bytes`           | raw part body                            |
| a `Struct`        | `json_decode(body, type=Struct)`         |
| a scalar          | `convert(text, type, strict=False)` — str / int / bool / `Literal` / enum |
| anything else     | **`WiringError`** (e.g. raw `dict`/`list` — JSON is always a Struct) |

`list[X]` = a part name that repeats (zero-or-more). `X | None` = optional part.
Both wrap either a bare type or a `FormPart[...]`.

### Polymorphic parts

Two supported patterns, author's choice:

- **Framework-validated** — `FormPart[A | B]` where `A`/`B` are msgspec
  **tagged unions** (`Struct, tag="..."`). msgspec picks the variant by the tag;
  bad shape → 422; `match form.x.data` is exhaustive; clean OpenAPI `oneOf`.
- **Self-decoded** — `FormPart[bytes]` (or bare `bytes`); the handler runs
  `json_decode(part.data, type=ChosenAtRuntime)`. With the
  `server-side-decode-returns-422` bug **fixed**, a decode failure here surfaces
  as **500** (server fault); if decoding *untrusted client* bytes, the handler
  raises `HTTPError(422, ...)` explicitly.

## Locked design decisions

- **`form` is body-exclusive.** Mutually exclusive with `json` and `content`
  (pick one), and forbidden on `_BODYLESS_VERBS` (GET/DELETE) — same WiringErrors
  as `json`/`content`.
- **`rename` honored.** A part's `name="..."` is the wire name; map to the field
  via `struct_fields(...).encode_name`, exactly as path binding does. A
  `rename="camel"` form Struct matches `name="jobType"` to `job_type`.
- **REST error mapping** (a form *is* a request body):
  - request `Content-Type` not `multipart/form-data` → **415** (new to jero;
    natural home for Unsupported Media Type).
  - malformed multipart framing → **400**.
  - well-formed body, schema failure (required part missing, `FilePart` part with
    no `filename`, JSON part failing validation, scalar failing coercion) → **422**.
  - JSON part failing *decode* (not validation) → **400**.
  All mapped **at the binder**, the sole producer of these 4xx (mirrors
  `_decode_json_body`); handler-side decode errors still fall through to 500.
- **`T` extraction reuses the factory trick.** `FormPart[JobConfig]` → `get_args`.
  `FilePart` → `get_original_bases(FilePart)` → `FormPart[bytes]` → `get_args` →
  `bytes`. Same mechanism as `_resolve_factory_type` for `BaseApp[Factory]`.
- **Whole body in RAM.** Reuses `_read_body` (buffer + join), then hands the bytes
  to `python-multipart`. `data: bytes` implies full buffering. No spooling/streaming
  in v1 (see Deferred).
- **Input-only.** Forms touch the binder only — never the response senders. No
  `form` return kind.

## Dependency

Add **`python-multipart`** as a runtime dependency (first runtime dep beyond
msgspec). Used only to parse a buffered body into parts (name, headers, filename,
content_type, body bytes). Note it in `AGENTS.md` "Layout"/deps when added.

## Wiring-time form spec

For each field of the `form` Struct, compile a descriptor:
- wire name (encode_name), required vs optional (`| None`), single vs `list`.
- enveloped (`FormPart[X]` / `FilePart`) vs bare.
- payload kind (bytes / struct / scalar) + the concrete decode type.
Reject unsupported payloads (raw dict/list, non-Struct JSON) with a precise
`WiringError`. All of this happens in `_bind_sources` / a new `_compile_form`.

## Per-request binder

1. Read & buffer the body (`_read_body`); require multipart content-type → else 415.
2. `python-multipart` → parts. Group by wire name.
3. For each field descriptor: gather matching parts; apply required/optional/list
   rules; build each value by payload kind; wrap in `FormPart`/`FilePart` (with
   `content_type`/`headers`, and `filename` for `FilePart` → 422 if absent).
4. Construct the form Struct; assign to `kwargs["form"]`.

## TestClient

Grow niquests-style multipart posting: `files=` (file parts) and `data=` (text/other
parts), e.g.
`client.post("/jobs", data={"jobType": "export-text"}, files={"document": ("in.pdf", b"...", "application/pdf")})`.
Encodes a `multipart/form-data` body with a boundary. (Verb-named methods
unchanged; this is just new kwargs on the existing ones.)

## Build stages (review each)

1. Public types (`FormPart[T]`, `FilePart`) in `jero/forms.py`; export from
   `jero`. Add `"form"` to `_SOURCES`; body-exclusivity + bodyless-verb
   `WiringError`s. Add `python-multipart` dep.
2. Wiring-time form spec: per-field classification (bare vs enveloped via
   `get_original_bases`/`get_args`; payload kind; optional/list; reject raw
   dict/list). Precise `WiringError` messages.
3. Per-request binder: buffered `python-multipart` parse, 415 on wrong
   content-type, match by encode_name, build each field, construct the Struct;
   400/422 mapping at the binder.
4. `TestClient` `files=` / `data=` multipart encoding.
5. Tests: file upload; `list[FilePart]`; `FormPart[Struct]` JSON part;
   `FormPart[bytes]` + bare `bytes`; `FormPart[scalar]` envelope (content_type
   read); bare scalar; optional-missing → None; required-missing → 422;
   non-multipart → 415; malformed framing → 400; camelCase field names
   (`rename`); JSON part bad shape → 422 / bad decode → 400; `FilePart` missing
   filename → 422.

## Deferred / notes

- **urlencoded** (`application/x-www-form-urlencoded`) — pure-scalar forms; would
  make `form` the complete HTML-form story (a `FilePart` in urlencoded → 415).
  Out of v1.
- **Resource vs Endpoint verb scope** — `form` works on any body verb (POST/PUT/
  PATCH) in both primitives; no special-casing planned, just not exhaustively
  spec'd here.
- **Large uploads** — whole body buffered in RAM. A spooled/streaming
  `FilePart` (temp file or `AsyncIterator[bytes]`) is a future change and would
  complicate the clean `data: bytes`. Defer until a real need.
- **Verify on 3.14 / pyright-strict**: msgspec supports a Struct subclassing a
  concrete parameterization (`class FilePart(FormPart[bytes])`); `T` resolves
  through the `FilePart → FormPart[bytes]` chain; a union `A | B` (tagged) as `T`
  satisfies decode (json_decode handles tagged unions natively).

---

## Amendment 1 (post-v1): typed / named part headers

> **Superseded by `plans/form-typed-headers.md`** — designed and locked there
> (the design pass found that nothing in jero exposes raw headers, so the v1
> raw `dict` is dropped rather than kept alongside). The notes below are the
> original sketch, retained for context.

**Do this after the staged build above ships and is tested — it's purely
additive.** Per the design-first convention, the final API needs a short design
chat before building; this section captures *intent + precedent*, not a locked
design.

### Motivation

Everywhere else in jero, headers bind into a **typed Struct**: the top-level
`headers` source declares `headers: MyHeaders` and the framework converts the
raw headers into it (name-mangled `x-trace-id` → `x_trace_id`, `rename`/encode_name
honored, failure → **400**). A form part is the one place headers are exposed only
as the raw `dict[str, str]` escape hatch. A part should be able to declare a typed
header Struct too — same ergonomics, same OpenAPI benefit, same fail-loud.

### Reuse (don't reinvent)

The mechanism already exists: `_convert_source(mangled, THeaders, 400)` plus the
`_raw_headers`-style `-`→`_` mangling used by the `headers` source. Part headers
are the same shape (a dict of header-name → value), so the typed binding is the
identical call with the identical status (**400**) and the identical `rename`
handling. The raw `dict[str, str]` stays as the escape hatch — **must not break
it** (the user explicitly wanted it kept).

### Candidate API (leading option, not locked)

A second generic parameter on `FormPart`, with a PEP 696 default so the common
"no typed headers" case stays a single bracket:

```python
class NoHeaders(Struct): ...                      # exported empty default

class FormPart[T, THeaders: Struct = NoHeaders](Struct):
    data: T
    content_type: str | None
    headers: dict[str, str]                       # raw escape hatch — unchanged
    named_headers: THeaders                       # typed view (NoHeaders when unparameterized)
```

Usage: `FormPart[JobConfig]` (no header typing, as today) or
`FormPart[JobConfig, UploadHeaders]` (`.named_headers.x_checksum`, validated, 400
on failure).

### Open questions for the design pass

- **Verbosity vs. precision.** A second generic param touches every wrapped field's
  annotation. Is the PEP 696 default enough to keep the common case clean, or is a
  separate opt-in shape better?
- **`FilePart`.** It's a *fixed* `FormPart[bytes]`. To let a file part declare typed
  headers it would itself need to become generic (`FilePart[THeaders]`) or expose a
  parameterized alias — resolve how without reintroducing the contradiction we
  removed (filename-required must stay).
- **Coexist vs. replace.** Confirm the raw `dict` and the typed view live side by
  side (recommended), and settle the typed field's name (`named_headers` vs.
  folding it onto `headers`).
- **Status + mangling parity.** Match the `headers` source exactly: 400 on convert
  failure, `-`→`_` mangling, `rename`/encode_name honored — verify against the live
  code at build time, don't assume.

### Dependency

Strictly after v1 (stages 1–6). Additive only; existing forms code and tests must
keep passing unchanged, and the raw `headers` dict must keep working.
