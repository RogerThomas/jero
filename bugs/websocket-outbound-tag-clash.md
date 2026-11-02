# An outbound WebSocket union with clashing tags passes wiring

## Symptom

```python
class A(Struct, tag="t"):
    a: int


class B(Struct, tag="t"):
    b: int


class ClashEndpoint(WebSocketEndpoint, path="/ws"):
    async def handle(self, websocket: WebSocket[Ping, A | B]) -> None: ...
```

wires without error. The same union inbound, or as a `json` body, is a `WiringError` (duplicate
tag value). A client cannot tell `A` from `B` on the wire.

## Where

`compile_websocket` in `jero/websockets.py` builds a decoder (and so trips msgspec's tag
validation) only for the inbound type; the outbound side only builds an encoder, which does not
check tags. `TestClient.websocket` decodes outbound frames and hits the error there instead.

## Fix direction

Validate the outbound union at wiring the same way (build and discard a decoder through
`json_frame_decoder`, or the shared Struct-union helper).
