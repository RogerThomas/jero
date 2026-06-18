# Links & Location

First-class `Location` (RFC 9110) and `Link` (RFC 8288) on responses, with **typed URL
reversal** to a mounted operation. Now that the path lives on the class, reversal is
cheap: the class is the handle.

## Shared core: a target

`Location` and `Link` are thin wrappers over one resolvable target. Two ways to build it:

- `from_operation(Class.operation, *, params=None)` — reverse-route to a mounted
  operation. The method reference *is* the operation (Endpoint `get`/… or Resource
  `read_one`/…), so it carries both the class (→ `path`) and the op (→ item/collection
  shape via `METHODS[op].extends_path`). `params` fills the `{slots}`.
- `from_url(url)` — a literal URL (relative or absolute), no reversal.

`Link` adds `rel` (required) plus optional RFC params (`title`, `type`); a list of them
joins into **one** `Link` header. `Location` is a single target, no `rel`.

```python
Location.from_operation(JobStatusEndpoint.get, params=JobPath(job_id=jid))
Link.from_operation(ConverseEndpoint.post, params=ConversationPath(conversation_id=cid), rel="converse")
Link.from_url("https://elsewhere/next", rel="next")
```

## Loud & fast: wrong params is caught at construction

The headline safety requirement. `from_operation(Class.op, params=...)` validates **at
construction** that `type(params)` matches that operation's declared `path` Struct —
introspected from the method's own signature, so it needs no app/registry and fails the
instant the wrong path object is used (in the handler, or even a unit test that just
builds the link). Mismatch → a clear `WiringError`/`TypeError`.

(Static pyright enforcement of `params` from a bare method ref isn't expressible, so this
is a hard *runtime* check at construction — immediate, not deferred to a served request.)

## Resolution & rendering (at response send)

- jero builds a wiring-time identity map `operation function → (class, op)` (it already
  iterates classes + ops in `_include`); render looks the method ref up there to get
  `class.path`, then fills slots from `params`. An operation that isn't a mounted route →
  loud error at render.
- **Relative by default** — `</jobs/job-1/status>` / `Location: /jobs/job-1` — always
  correct and RFC-valid, and dodges proxy-host ambiguity. Absolute (`https://host/…`,
  from the request scope or a configured `base_url`) is opt-in. (Opt-in mechanism: a
  build-time detail — per-call flag vs app `base_url`.)

## Placement

New params on the response wrappers, parallel to `headers` / `status_code`, and on the
streaming responses for symmetry:

- `location: Location | None = None`
- `links: list[Link]` (default empty)

## What it unlocks

- **`Location` on 201** (closes the roadmap item): `JSONResponse(json=w, status_code=201,
  location=Location.from_operation(Widgets.read_one, params=WidgetPath(widget_id=w.id)))`.
- **Redirects**: `status_code=303, location=Location.from_operation(...)`.
- **Async jobs**: `status_code=202, location=Location.from_operation(JobStatusEndpoint.get, params=...)`.

## Decisions

- **Locked:** factory constructors (`from_operation` / `from_url`), unified
  `from_operation` (no separate endpoint/resource forms — the method ref disambiguates),
  one `Link` header / single `Location`, params validated at construction, relative URLs
  by default.
- **Build-time detail:** the absolute-URL opt-in mechanism; whether the wiring identity
  map is exposed for testing.
- **Non-goal (v1):** the same class mounted at two paths (forbidden anyway — one class,
  one path), and `Link`/`Location` on error responses.
