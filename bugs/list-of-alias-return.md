# `-> list[Alias]` is rejected as a return type

## Symptom

```python
class W(Struct):
    a: int


type WAlias = W


class ListAliasEndpoint(Endpoint, path="/x"):
    async def get(self) -> list[WAlias]: ...
```

fails wiring with "must declare a return type of Struct, list[Struct], ...", although
`-> WAlias` and `-> list[W]` both wire.

## Where

`_return_kind` in `jero/core.py` resolves an alias at the top level of the return annotation
but checks a `list[T]` item with `isinstance(args[0], type)` without resolving `T`.

## Fix direction

Run the item through `unwrap_alias` before the Struct check.
