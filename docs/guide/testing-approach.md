# Testing approach

jero's test suite runs against **one shared application** — the
[`demo_app/`](https://github.com/RogerThomas/jero/tree/main/demo_app) package — and that
single app plays three roles at once:

1. **Documentation and reference.** It's the project-structured worked example the docs
   point to (see the [complete example](complete-example.md)) — a real `config` / `models` /
   `services` / `operations` / `factory` / `app` layout, not a throwaway snippet. If you're
   sizing jero up, read `demo_app/` end to end: it's the clearest picture of how an API
   built on jero actually fits together — where the framework boundary sits, how
   dependencies get wired, and what idiomatic jero looks like at project scale rather than
   in a single-file example.
2. **The integration-test fixture.** Most of the suite drives `demo_app` through the
   `TestClient`, mocking only the I/O service layer via the public `factory=` seam. Tests
   that need esoteric wiring (streaming, response kinds, wiring errors) still build their
   own small apps locally.
3. **A typed consumer of the public API.** Because `demo_app` uses jero exactly the way
   you would, it is type-checked alongside `./tests` by every major type checker.

Keeping all three in one artifact is deliberate: it makes `demo_app` a **single source of
truth** for what jero is and how it behaves.

## Test only the public interface

`demo_app` and the suite exercise **only the public, user-facing surface** — the
`TestClient`, the exported `Resource` / `Endpoint` / `BaseApp` types, and the documented
binding and response contracts. Nothing reaches into private internals.

- **Refactor freely.** Internals can be reshaped entirely; as long as the public contract
  holds, the suite stays green. The tests pin *behaviour*, not implementation detail.
- **The suite is the contract.** A passing run means the user-facing behaviour is correct
  — exactly the guarantee jero's users care about.

## Why one source of truth pays off

- **The docs can't drift from reality.** The example you read is the code the suite
  executes — if it broke, CI would be red. No stale, copy-pasted snippets that quietly
  rot out of date.
- **Living, exhaustive coverage.** `demo_app` is run on every commit, so it can't rot;
  every feature it demonstrates is a feature under test.
- **Dogfooding.** Building and maintaining a realistic, structured app surfaces ergonomic
  rough edges in jero's own API before users hit them.
- **Type-checked from every angle.** As a typed consumer, `demo_app` and `./tests` are
  checked by mypy, ty, pyright, and zuban (see
  [Strictly typed, every checker](../philosophy.md#strictly-typed-every-checker)) — so the
  public API is guaranteed to hold up under whichever type checker *you* use.
