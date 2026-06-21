# Testing approach

jero's own test suite lives in `./tests`, and it is written deliberately against
**only the public, user-facing interface** — the same surface you build on. The tests
drive the app through its public API (the `TestClient`, the exported `Resource` /
`Endpoint` / `BaseApp` types, and the documented binding and response contracts) and
never reach into private internals.

## Why only the public interface

- **Refactor freely.** Internals can be reshaped completely; as long as the public
  contract still holds, the suite stays green. The tests pin *behaviour*, not
  implementation detail.
- **The suite is the contract.** A passing run means the user-facing behaviour is
  correct — which is exactly the guarantee jero's users care about.
- **One honest surface.** `./tests` is the canonical, executable record of what jero
  promises. If a behaviour isn't exercised there, it isn't part of the contract.

## A typed consumer of the public API

Because `./tests` uses jero exactly the way you would, it doubles as a fully typed
*consumer* of the public API. That is what lets jero type-check the entire public
surface — see [Strictly typed, every checker](../philosophy.md#strictly-typed-every-checker) —
with mypy, ty, pyright, and zuban in CI. If the public interface trips up any of them,
it's caught before release.
