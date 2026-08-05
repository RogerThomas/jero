# REST semantics

jero follows REST/HTTP semantics out of the box — the status codes, `HEAD`, and
`OPTIONS` are handled for you, consistently, so you don't hand-roll them per route.

Failures are typed errors you raise — that whole story (Problem Details, your own
error classes, custom body formats, exception handlers) lives in [Errors](errors.md).
This page covers the semantics the framework applies on its own.

## Status codes

| Situation                                            | Status |
| ---------------------------------------------------- | ------ |
| Unmatched URL                                        | 404    |
| Path value that fails conversion to its field type   | 404    |
| Malformed query string or headers                    | 400    |
| Malformed JSON body                                  | 400    |
| Well-formed body that fails the schema               | 422    |
| Auth failure                                        | 401    |
| Wrong method for a known path                        | 405 (with `Allow`) |
| Unsupported media type where a form is expected      | 415    |
| `create` success                                     | 201    |
| Other success                                        | 200    |

The split between **400** (malformed — can't even parse) and **422** (well-formed but
fails validation) follows the binding source: a body that isn't valid JSON is 400;
valid JSON that doesn't match the `Struct` is 422. A bad *path* value is 404, because a
segment that doesn't convert doesn't identify a resource.

These framework decode/validation errors don't just report *that* something failed —
they surface msgspec's own message as the human `detail`, with the same string available,
typed, under `params.reason`:

```json
{
  "type": "validation-failed", "title": "Validation failed", "status": 422,
  "detail": "Expected `int`, got `str` - at `$.priceCents`",
  "params": {"reason": "Expected `int`, got `str` - at `$.priceCents`"}
}
```

The message names the failing field and its JSON path, never the submitted value, so it
is safe to return; it is for humans and logs, so keep dispatching on `type`, not on
parsing `detail`. msgspec stops at the first invalid field, so a response reports one
failure at a time rather than a collected list.

## HEAD and OPTIONS

These are synthesized; you never write them:

- **`HEAD`** is served from the matching `GET` route with the body suppressed (and a
  streaming `GET` is *not* iterated for a `HEAD`).
- **`OPTIONS`** answers `204` with an `Allow` header listing the methods for that path.
- A **`405`** likewise carries an `Allow` header. `Allow` always includes `OPTIONS`, and
  `HEAD` wherever `GET` is available.

## Custom status

Override the default success status per response with `status_code` on a response
wrapper — see [Responses & headers](responses.md#status-codes).

## Why this is fixed, not configurable

These semantics are part of jero being opinionated: there's one correct mapping, the
framework encodes it, and "what status should this return?" never reaches code review.
Everything here is resolved at startup or by fixed rules — nothing adds work to the
request path.
