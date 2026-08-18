# Plan: Spooled file uploads (streamed multipart, bounded memory)

Status: **designed, not built.** No code exists. Build only when a real user needs
large uploads through jero itself; the presigned-URL pattern (below) remains the
recommended answer and should be documented regardless.

## Goal

Let a handler accept large file uploads without the body ever being fully resident in
memory: parts buffer in RAM below a threshold and roll over to disk above it, with the
buffering policy declared in the handler's types, decided at wiring, and cleaned up
per request.

## Today (why this doesn't exist yet)

The form path is fully eager, in three steps:

1. The ASGI receive loop buffers the entire request body into memory
   (`core.py`, the inlined body read in the operation handler).
2. The complete body is wrapped in `BytesIO` and handed to `MultipartParser`
   (`core.py`, the form decode path).
3. Each part's `.raw` bytes are copied into `FilePart.data: bytes`.

A 500MB upload is therefore roughly 2x 500MB of transient RAM per request. jero also
imposes no body-size cap of its own; only the server or proxy in front bounds it.
For the typed JSON APIs jero targets, eager small bodies are the right default; this
plan changes nothing about the existing `FilePart`.

## Design

### 1. The spooled container: stdlib, nothing to build

`tempfile.SpooledTemporaryFile` already does memory-below-threshold, disk-above.
Better: the `multipart` dependency's parser has per-part spooling built in
(memory limits with disk rollover). Today jero defeats it by pre-buffering the whole
body and materializing `.raw`.

### 2. Streaming the body into the parser: the real work

Spooling is pointless if the whole body was already buffered to produce it. Switch
the form path to the package's `PushMultipartParser` (its sans-io push interface):
feed chunks from `receive` as they arrive; spool each part's payload past the
threshold. Disk writes stay off the event loop (thread offload per flush, the same
trade discussed for the deferred asset disk tier in `_include_assets`). This is a
rewrite of the multipart request path, not a patch to it.

### 3. The typed contract: declare the policy, don't configure it

`FilePart.data: bytes` *is* the eager policy, declared in the type. Spooling is a
different declaration, not a flag that changes what `data` means:

```python
class SpooledFilePart[H: Struct | None = None](Struct):
    file: ...       # file-like positioned at 0; on disk iff it rolled over
    filename: str
    content_type: str | None
    headers: H
    raw_headers: RawHeaders
```

A handler declaring `SpooledFilePart` gets the streaming path; one declaring
`FilePart` keeps today's eager bytes. The annotation picks the machinery at wiring,
like every other jero contract. Mixing both kinds in one form is allowed; the parser
spools only the parts declared spooled.

### 4. Request-scoped cleanup: the one genuinely new concept

jero currently has zero per-request resources. Spooled temp files must be deleted
after the response completes (careful: a streaming response may still be reading the
file). Needs a small finally-after-send hook in the operation handler; wiring-time
detection means only routes that declared a spooled part pay for it.

### 5. Bundle a body cap: `max_body_bytes`

Spooling protects RAM; without a cap it just moves unbounded input to disk. The same
work should add a per-include (or per-app) `max_body_bytes` that answers 413 once
exceeded, enforced in the receive loop for every body kind, not just forms. Arguably
this piece is worth shipping *before* spooling.

## Stages

1. `max_body_bytes` on the receive loop, 413 semantics, tests, docs. Independent and
   valuable alone.
2. `SpooledFilePart` type + wiring detection (route compiles to the streaming path
   only when declared) + `PushMultipartParser` feed + per-part spool.
3. Request-scoped cleanup hook; interaction tests with streaming responses.
4. Docs page section in `guide/forms.md` + FAQ entry; a "large uploads" note
   recommending presigned URLs to object storage as the default architecture, with
   spooled parts as the escape hatch.

Estimate: 3-5 days at repo quality bar; most of it in stages 2-3.

## Explicitly out of scope

- Changing `FilePart` semantics in any way.
- Multipart response bodies, resumable uploads, upload progress.
- A generic spooled request body (`content: SpooledBody`); same machinery could
  support it later if a real case appears, but forms are the motivating shape.
