# Testing approach

jero's test suite runs against **one shared application**, the
[`demo_app/`](https://github.com/RogerThomas/jero/tree/main/demo_app) package. That one
app plays three roles:

1. **Documentation and reference.** It is the project-structured worked example the
   docs point to (see the [complete example](complete-example.md)): a real `config` /
   `models` / `services` / `operations` / `factory` / `app` layout. If you're sizing
   jero up, read `demo_app/` end to end.
2. **The integration-test fixture.** Most of the suite drives `demo_app` through the
   `TestClient`, mocking only the I/O service layer via the public `factory=` seam.
   Tests that need esoteric wiring (streaming, response kinds, wiring errors) build
   their own small apps locally.
3. **A typed consumer of the public API.** `demo_app` uses jero exactly the way you
   would, so it is type-checked alongside `./tests` by mypy, ty, pyright, and zuban
   (see [Strictly typed, every checker](../philosophy.md#strictly-typed-every-checker)).

What that buys:

- **The docs can't drift.** The example you read is the code the suite executes on
  every commit. If it broke, CI would be red.
- **Dogfooding.** Maintaining a realistic, structured app surfaces rough edges in
  jero's own API before users hit them.

## Test only the public interface

`demo_app` and the suite exercise **only the public, user-facing surface**: the
`TestClient`, the exported `Resource` / `Endpoint` / `BaseApp` types, and the documented
binding and response contracts. Nothing reaches into private internals.

- **Refactor freely.** Internals can be reshaped entirely. As long as the public
  contract holds, the suite stays green.
- **The suite is the contract.** A passing run means the user-facing behaviour is
  correct.
