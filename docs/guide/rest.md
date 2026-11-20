# REST semantics

jero applies REST/HTTP semantics for you: status codes, `HEAD`, and `OPTIONS` are
handled the same way on every route. The mapping is fixed, not configurable.

This page covers what the framework does on its own. Failures you raise yourself
(Problem Details, your own error classes, custom body formats, exception handlers) are
in [Errors](errors.md).

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

How 400, 422, and 404 divide:

- **400: the request couldn't be read.** Malformed JSON, a malformed multipart body, or
  a query or header value that doesn't convert to its field type.
- **422: it was read, but breaks the schema.** A successfully parsed body or form part
  whose content doesn't match its declared `Struct`. So a bad value in the query string
  is a 400, and the same bad value inside a JSON body is a 422.
- **404: a bad path value.** A segment that doesn't convert to its field type doesn't
  identify a resource.

These errors carry msgspec's own message as the human `detail`, and the same string,
typed, under `params.reason`:

```json
{
  "type": "validation-failed", "title": "Validation failed", "status": 422,
  "detail": "Expected `int`, got `str` - at `$.priceCents`",
  "params": {"reason": "Expected `int`, got `str` - at `$.priceCents`"}
}
```

- The message names the failing field and its JSON path, never the submitted value, so
  it is safe to return.
- It is for humans and logs. Dispatch on `type`, not on parsing `detail`.
- msgspec stops at the first invalid field, so a response reports one failure at a time,
  not a collected list.

## HEAD and OPTIONS

These are synthesized; you never write them:

- **`HEAD`** is served from the matching `GET` route with the body suppressed (and a
  streaming `GET` is *not* iterated for a `HEAD`).
- **`OPTIONS`** answers `204` with an `Allow` header listing the methods for that path.
- A **`405`** likewise carries an `Allow` header. `Allow` always includes `OPTIONS`, and
  `HEAD` wherever `GET` is available.

## Custom status

Two ways to answer with a different success status, both on
[Responses & headers](responses.md#status-codes):

- Return a `NoContent` (204), `Created` (201), or `Accepted` (202) wrapper.
- Set `status_code` on any response wrapper for any other status.
