# Form-field Struct-union members lose their ModelMeta in the spec

## Symptom

A `jero.Struct` member with `meta=ModelMeta(description=...)` keeps its description when it
appears in a `json` union body, but not when it appears in a form field's Struct union:

```python
class Described(Struct, tag="d", meta=ModelMeta(description="member-description")):
    a: int


class Second(Struct, tag="e"):
    b: int


class DescribedForm(msgspec.Struct):
    pick: Described | Second
```

`components.schemas.Described` has no `description`.

## Where

`_collect_types` in `jero/openapi.py` collects a `json` union member by member through
`_model_payload_types`, but adds a form field's payload type as one union, so the per-member
`ModelMeta` pass never sees the members.

## Fix direction

Route form-field payloads through `_model_payload_types` as well.
