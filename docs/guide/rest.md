# REST & error semantics

jero follows REST/HTTP semantics out of the box — the status codes, `HEAD`, and
`OPTIONS` are handled for you, consistently, so you don't hand-roll them per route.

## Status codes

| Situation                                            | Status |
| ---------------------------------------------------- | ------ |
| Unmatched URL                                        | 404    |
| Path value that fails conversion to its field type   | 404    |
| Malformed query string or headers                    | 400    |
| Malformed JSON body                                  | 400    |
| Well-formed body that fails the schema               | 422    |
| Auth failure (`HTTPError(401, …)`)                   | 401    |
| Wrong method for a known path                        | 405 (with `Allow`) |
| Unsupported media type where a form is expected      | 415    |
| `create` success                                     | 201    |
| Other success                                        | 200    |

The split between **400** (malformed — can't even parse) and **422** (well-formed but
fails validation) is deliberate and follows the binding source: a body that isn't valid
JSON is 400; valid JSON that doesn't match the `Struct` is 422. A bad *path* value is
404, because a segment that doesn't convert doesn't identify a resource.

## Raising errors

Raise `HTTPError(status, detail)` from anywhere in a handler (or an authenticator, or a
service) to short-circuit:

```python
from jero import HTTPError

raise HTTPError(404, "widget not found")
# -> 404  {"error": "widget not found"}
```

Any other uncaught exception becomes a `500` with a generic body — your internals never
leak to the client.

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
