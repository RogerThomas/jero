<div align="center">

<img src="docs/assets/jero-logo.png" alt="jero" width="440">

<p>
  <a href="https://img.shields.io/github/v/release/RogerThomas/jero"><img src="https://img.shields.io/github/v/release/RogerThomas/jero" alt="Release"></a>
  <a href="https://github.com/RogerThomas/jero/actions/workflows/main.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/RogerThomas/jero/main.yml?branch=main" alt="Build status"></a>
  <a href="https://codecov.io/gh/RogerThomas/jero"><img src="https://codecov.io/gh/RogerThomas/jero/branch/main/graph/badge.svg" alt="codecov"></a>
  <a href="https://img.shields.io/github/commit-activity/m/RogerThomas/jero"><img src="https://img.shields.io/github/commit-activity/m/RogerThomas/jero" alt="Commit activity"></a>
  <a href="https://img.shields.io/github/license/RogerThomas/jero"><img src="https://img.shields.io/github/license/RogerThomas/jero" alt="License"></a>
</p>

**An opinionated, msgspec-first ASGI micro-framework for Python 3.14.**

<a href="https://github.com/RogerThomas/jero/">GitHub</a> · <a href="https://RogerThomas.github.io/jero/">Documentation</a>

</div>

## What is jero?

jero is built on three non-negotiable pillars:

1. **Speed.** All introspection happens once, at wiring time. The per-request path
   is just dict lookup → msgspec decode → call → encode — nothing else.
2. **Opinionated, scaffolded DX.** There is one blessed way to do each thing.
   Contracts are checked at startup and fail loud with a precise `WiringError`,
   never silently at runtime.
3. **Strict, expressive typing.** Everything is fully, statically typed under
   pyright-strict. Types *are* the contract — the binding, the wiring errors, and
   (on the roadmap) the source of the OpenAPI spec.

It deliberately has **no DI container**: you hand-wire dependencies in `_wire`, and
the framework owns only what the language doesn't give you for free — lifecycle.

## Example

```python
from msgspec import Struct

from jero import BaseApp, Endpoint, Resource, TestClient


class Widget(Struct):
    id: str
    name: str


class WidgetResource(Resource):
    async def read_one(self, path: "WidgetPath") -> Widget:
        return Widget(id=path.widget_id, name="widget-name")


class WidgetPath(Struct):
    widget_id: str


class HealthEndpoint(Endpoint):
    async def get(self) -> Widget:
        return Widget(id="health", name="ok")


class App(BaseApp):
    async def _wire(self) -> None:
        self._include_resource(WidgetResource(), path="/widgets")
        self._include_endpoint(HealthEndpoint(), path="/healthz")


app = App()

# Test it in-process — no socket, no server:
with TestClient(app) as client:
    resp = client.get("/widgets/abc")
    assert resp.status_code == 200
    assert resp.json() == {"id": "abc", "name": "widget-name"}
```

Run it under any ASGI server, e.g. [granian](https://github.com/emmett-framework/granian):

```bash
granian --interface asgi myapp:app
```

## Development

```bash
task install   # create the venv and install pre-commit hooks
task check     # lock check + ruff, pyright, deptry, pylint (via prek)
task test      # run the test suite with coverage
```

See [`AGENTS.md`](AGENTS.md) for the design philosophy and the contract, and
[`style-guide.md`](style-guide.md) for project conventions.

## Releasing a new version

Publishing uses PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — no token required.

1. Bump `version` in `pyproject.toml` and commit it to `main`.
2. Create a [GitHub release](https://github.com/RogerThomas/jero/releases/new)
   tagged with the **same** version — a bare PEP 440 string, e.g. `0.1.0` (no `v`).

The release workflow verifies the tag matches `pyproject.toml`, builds, publishes
to PyPI, and deploys the docs. A version mismatch fails the release.

---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).
