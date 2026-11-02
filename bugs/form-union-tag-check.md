# Form-field Struct unions skip the tag check and the WiringError rewrap

## Symptom

A form field typed as a Struct union does not go through `require_string_tags` or
`build_decoder`, unlike a `json` body or an inbound WebSocket frame:

```python
class Plain(Struct):
    a: int


class Other(Struct):
    b: int


class PickForm(Struct):
    pick: Plain | Other
```

Wiring fails with msgspec's raw `TypeError` ("If a type union contains multiple Struct types,
all Struct types must be tagged ...") instead of a `WiringError` naming the endpoint and field.
A union whose members all carry `int` tags is accepted, though the same union as a `json` body
is a `WiringError` (string tags only).

## Where

`_compile_form` in `jero/core.py` calls `decoder_for(payload_type)` directly for a Struct-union
payload. The `json` path (`_json_annotation` + `_json_decoder`) runs `require_string_tags` and
`build_decoder` first.

## Fix direction

One shared Struct-union helper in `_wiring_types` that the `json`, form-field, and WebSocket
sites all call (tag check, compose, decoder build, rewrap), so the rules can't drift per site.
