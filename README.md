# jero

[![Release](https://img.shields.io/github/v/release/RogerThomas/jero)](https://img.shields.io/github/v/release/RogerThomas/jero)
[![Build status](https://img.shields.io/github/actions/workflow/status/RogerThomas/jero/main.yml?branch=main)](https://github.com/RogerThomas/jero/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/RogerThomas/jero/branch/main/graph/badge.svg)](https://codecov.io/gh/RogerThomas/jero)
[![Commit activity](https://img.shields.io/github/commit-activity/m/RogerThomas/jero)](https://img.shields.io/github/commit-activity/m/RogerThomas/jero)
[![License](https://img.shields.io/github/license/RogerThomas/jero)](https://img.shields.io/github/license/RogerThomas/jero)

An opinionated, msgspec-first ASGI micro-framework for Python 3.14.

- **GitHub**: <https://github.com/RogerThomas/jero/>
- **Documentation**: <https://RogerThomas.github.io/jero/>

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

- Create an API token on [PyPI](https://pypi.org/) and add it to the repo secrets
  as `PYPI_TOKEN`.
- Create a [new release](https://github.com/RogerThomas/jero/releases/new) on
  GitHub with a tag of the form `*.*.*`.

The release workflow builds, publishes to PyPI, and deploys the docs.

---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).
