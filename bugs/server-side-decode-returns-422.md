# Bug: server-side msgspec decode failures return 422/400 instead of 500

**Severity:** medium (wrong status code → clients/monitoring treat a server fault
as a client error; masks upstream outages as "bad request").

## Summary

`_Route.__call__` wraps **both** request binding **and the user's handler body**
in one `try`, and broadly catches `msgspec.ValidationError → 422` and
`msgspec.DecodeError → 400`. So when a *handler/service* does its own
`msgspec.json.decode(...)` — e.g. decoding an upstream API response — and that
fails, the framework reports it as a **client** error (422/400) when it is a
**server-side** failure that should be **500**.

## Where

`jero/core.py`, `_Route.__call__` (~lines 605–620):

```python
try:
    kwargs = await self._bind(scope, receive, path_values)
    result = await self._fn(**kwargs) if self._is_async else self._fn(**kwargs)
except HTTPError as exc:
    ... exc.status ...
except ValidationError as exc:
    await _send_json(send, 422, ...)   # ← also catches errors from self._fn
except DecodeError as exc:
    await _send_json(send, 400, ...)   # ← also catches errors from self._fn
```

The over-broad `except` blocks cover `self._fn(**kwargs)` (user code), not just
binding.

Contributing detail: the request-body decode in the binder (`jero/core.py` ~504)
raises **raw** `ValidationError`/`DecodeError` and *relies on* that outer `except`
to produce 422/400:
```python
kwargs["json"] = json_decode(await _read_body(receive), type=self._json_type)
```
By contrast `_convert_source` (~228–237, used for params/headers/path) correctly
wraps its `ValidationError` into `HTTPError(...)` at the binder. The JSON body
path doesn't, which is *why* the broad outer catch exists — and that catch is
what leaks onto the handler.

## Repro

A service that decodes an upstream response (real examples in the repo:
`api/better.py` `PokemonService.fetch` and `tests/factory_app.py`
`MovieService.get_movie`, both do `json_decode(resp.content, type=...)`):

```python
class Thing(Struct):
    id: int

class R(Resource):
    async def read_many(self) -> Thing:
        # upstream returned an unexpected shape; client's request was perfectly valid
        return json_decode(b'{"id": "not-an-int"}', type=Thing)  # raises ValidationError
```
Request a valid GET → **actual: 422** (`{"error": "Expected int ..."}`).
**Expected: 500** — the client did nothing wrong; our server/upstream did.

(A malformed-JSON upstream → `DecodeError` → currently a wrong **400**, same root cause.)

## Expected behaviour

- **Client input** that fails decode/validation → 4xx (unchanged): malformed
  request JSON → 400; well-formed body failing the schema → 422; bad query/headers
  → 400; bad path value → 404.
- **Any `ValidationError`/`DecodeError` raised inside the handler/service** →
  **500** (server error). The client's request was valid.

## Fix direction

1. **Wrap the request-body decode in the binder** so client-body errors are
   mapped to their HTTP status *at the binder* (the sole place that turns decode
   failures into 4xx). It must distinguish: `DecodeError` (malformed JSON) → 400,
   `ValidationError` (well-formed, wrong schema) → 422. e.g. a small helper:
   ```python
   try:
       kwargs["json"] = json_decode(await _read_body(receive), type=self._json_type)
   except DecodeError as exc:
       raise HTTPError(400, str(exc)) from None
   except ValidationError as exc:
       raise HTTPError(422, str(exc)) from None
   ```
2. **Remove the broad `except ValidationError` / `except DecodeError` from
   `_Route.__call__`.** Keep only `except HTTPError`. Then a decode/validation
   error escaping `self._fn` propagates uncaught → becomes a 500.

After this, the only producers of 400/422 are the binder (client input); handler
errors fall through to the 500 path.

## Related (separate decision, not required for the fix)

Today an uncaught handler exception propagates past `__call__` to the ASGI server
(granian), which returns a 500 with no JSON body. Consider a framework-level 500
handler that emits a consistent `{"error": "internal server error"}` (and logs
the traceback) so server errors match the JSON error shape used elsewhere. If
added, the fix above should let `ValidationError`/`DecodeError` reach it.

## Tests to add (`tests/`)

- Handler that `json_decode`s bad upstream bytes → response is **500** (not 422/400).
- Regression: malformed **request** body → still 400; valid JSON, wrong **request**
  schema → still 422; bad query/header → 400; bad path value → 404.
