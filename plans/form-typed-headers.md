# Plan: Typed (named) form part headers

Status: **designed, not built.** Builds on the shipped `form` source
(`plans/forms.md`, now built). Supersedes "Amendment 1" in `plans/forms.md`.

## The discovery that drove the design

Nothing in jero ever hands a handler **raw** headers. The `headers` source binds
*only* into a typed Struct — `_convert_source(raw_headers, self._headers_type, 400)`
(core.py:811) — and even auth converts the raw dict straight into the
authenticator's typed `headers` Struct. The raw dict is internal scratch, never
exposed. That is deliberate (pillar 3: no untyped `dict` I/O).

So `FormPart.headers: dict[str, str]` from forms v1 is the **single place in all
of jero that leaks a raw header dict** — the lone untyped-I/O hole and the only
inconsistency with how headers work everywhere else. This feature closes it.

## Goal

A form part's headers become a **typed Struct**, exactly like every other
`headers` in jero (mangled, validated, `rename`-honored, failure → 400). Opt-in
via a second generic parameter with a PEP 696 default, so the common case is
unchanged.

## Public API (the change)

```python
# jero/forms.py

class NoHeaders(Struct): ...                          # exported empty default

class FormPart[T, H: Struct = NoHeaders](Struct):
    data: T
    content_type: str | None
    headers: H                       # WAS dict[str, str]; now typed, NoHeaders() by default

class FilePart[H: Struct = NoHeaders](FormPart[bytes, H]):
    filename: str
```

Exported from `jero`: add `NoHeaders` (alongside `FormPart`, `FilePart`).

`content_type` stays — it's a clean `str | None` (not a dict) and the one header
every part wants. **The raw `headers: dict[str, str]` is removed** (behavior change
to v1; pre-release, deliberate — see below).

## How an app author uses it

```python
class UploadHeaders(Struct):
    x_checksum: str                  # matches "X-Checksum" via lower + '-'→'_'

class CreateJob(Struct):
    document: FilePart                          # H = NoHeaders — spelling unchanged
    upload:   FilePart[UploadHeaders]           # upload.headers.x_checksum (validated)
    blob:     FormPart[bytes, UploadHeaders]    # blob.headers.x_checksum
    options:  FormPart[JobConfig]               # T only, H defaults
```

The default means **every existing form annotation keeps working untouched**; the
generic only appears when someone opts into typed headers.

## Locked decisions

1. **No raw dict.** `FormPart.headers` *is* the typed Struct `H`, matching the
   `headers` source everywhere. Removes the v1 untyped-dict hole. A part with
   headers you didn't model → you don't get them untyped (same framework-wide
   stance as elsewhere).
2. **`FilePart[H]` is generic too** — file parts get typed-header parity (no
   second-class part type). `document: FilePart` still works via the default.
3. **`NoHeaders` empty-Struct default** so `.headers` is *always* a Struct — no
   `None` checks, no `H | None`.
4. **Parity with the `headers` source:** convert failure → **400** (part headers
   are metadata, not body schema → not 422); mangling is lower + `-`→`_`;
   `rename`/encode_name honored through `convert`.

## Implementation (on top of built forms code)

- **`jero/forms.py`:** add `NoHeaders`; add the `H` param to `FormPart` and make
  `FilePart` generic on `H`; change `headers` field type to `H`.
- **`jero/__init__.py`:** export `NoHeaders`.
- **Type extraction** (`_form_part_payload`, core.py:327) → return **both** the
  payload type `T` *and* the headers type `H`. On 3.14, `get_args` **materializes
  the PEP 696 default** (confirmed empirically — see Verify), so branch on
  `get_origin`, no `get_original_bases` needed for the subscripted forms:
  - `get_origin is FormPart` → `get_args` is `(T, H)` (default already filled) ⇒
    `T=args[0]`, `H=args[1]`.
  - `get_origin is FilePart` (i.e. `FilePart[H]`) → `get_args` is `(H,)` ⇒
    `T=bytes`, `H=args[0]`.
  - bare `FilePart` (unsubscripted, `get_args` empty) ⇒ `T=bytes`, `H` from the
    type param default: `FilePart.__type_params__[0].__default__` (→ `NoHeaders`).
  ```python
  def _form_part_types(ann: object) -> tuple[object, object] | None:
      origin = get_origin(ann)
      if origin is FormPart:
          args = get_args(ann)
          return (args[0], args[1]) if len(args) == 2 else None
      if origin is FilePart:
          args = get_args(ann)
          return (bytes, args[0]) if len(args) == 1 else None
      if ann is FilePart:
          return bytes, FilePart.__type_params__[0].__default__
      return None
  ```
- **`_FormField`** (core.py:374) → add `headers_type: type[Struct]`.
- **`_compile_form`** (core.py:400) → populate `headers_type`. (No new
  `WiringError` needed — `H: Struct` bound is enforced by the type system; a
  non-Struct would already fail `_struct_annotation`-style checks if we add one.
  Decide whether to validate `H` is a Struct at wiring for a friendly message.)
- **`_decode_form_value`** (core.py:490) → before constructing the part, build the
  typed headers: mangle `part.headers` (lower + `-`→`_`, mirroring `_raw_headers`,
  core.py:244) and `_convert_source(mangled, field.headers_type, 400)`. Pass the
  result as `headers=` to `FormPart`/`FilePart`. When `headers_type is NoHeaders`,
  skip the convert and pass `NoHeaders()` (cheap, avoids per-part work for the
  common case — pillar 1).

## Verify at build (don't assume)

- ~~`get_args` returns only explicit args~~ — **resolved: false on 3.14.** It
  materializes the PEP 696 default, so `FormPart[bytes]` → `(bytes, NoHeaders)`.
  The extraction recipe above reflects this. (Bare `FilePart` still has empty
  `get_args` → use the type param default; confirm `__type_params__[0].__default__`
  is `NoHeaders` at build.)
- `convert(values, form_struct)` accepting a pre-built **two-param**
  `FormPart`/`FilePart` instance for its field — **confirmed working** during the
  first build attempt.
- `FilePart[H]` (generic Struct subclassing a partially-parameterized generic base
  `FormPart[bytes, H]`) is accepted by msgspec and pyright-strict.

## Tests (extend `tests/test_forms.py`)

- **Update v1 tests for the dropped raw dict** — current tests read
  `.content_type` (kept) but none read `.headers` as a dict; confirm none rely on
  the removed field, adjust if so.
- Typed headers on a `FormPart[bytes, H]` → `.headers.x_checksum` populated &
  validated; camelCase/kebab header name mangling (`X-Checksum` → `x_checksum`).
- Typed headers on a `FilePart[H]` (parity).
- Missing required typed header → **400** (not 422).
- Bare `FilePart` / `FormPart[T]` → `.headers` is `NoHeaders()` (default path,
  no convert run).

## Out of scope

- A typed `content_type` enum or per-part header *defaults* beyond what the Struct
  itself expresses — model in `H` if wanted.
- Re-exposing any raw header dict — intentionally gone.
