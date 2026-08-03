# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "blacksheep",
#     "fastapi",
#     "jero",
#     "litestar",
#     "msgspec",
#     "rich",
# ]
# ///
"""In-process ASGI micro-benchmark: jero vs Litestar vs BlackSheep vs FastAPI vs a
hand-rolled raw ASGI app, all serving the same three-endpoint API.

Every app is driven directly as an ASGI callable — no server, no sockets — so what you
measure is each framework's own per-request overhead: routing, binding/validation, and
serialization. That makes the numbers a per-core ceiling, not a production promise.

Scenarios (identical raw ASGI scopes for every app):
  plain   GET  /ping                      -> tiny JSON body
  bind    GET  /items/{item_id}?limit=10  -> path + query binding, JSON body
  body    POST /items (JSON, 3 fields)    -> body decode/validation, JSON echo

Fairness notes:
  - The "raw ASGI" app uses no framework but still does honest work with msgspec:
    hand-rolled routing, path/query extraction, a typed validating decode of the POST
    body, and a typed encode of every response. It skips everything a framework gives
    you (404/405 semantics, HEAD/OPTIONS, content-type checks, error envelopes).
  - Each framework app is written the way its own documentation recommends.
  - BlackSheep uses its documented msgspec JSON hook (its fastest configuration).
  - Tweak freely: pin versions in the dependencies block above, change N_MEASURE,
    add a framework, add a scenario.
"""

import asyncio
import time

N_WARMUP = 2_000
N_MEASURE = 20_000
BEST_OF = 3

POST_BODY_CAMEL = b'{"name":"gizmo","priceCents":499,"tags":["a","b"]}'
POST_BODY_SNAKE = b'{"name":"gizmo","price_cents":499,"tags":["a","b"]}'


# --------------------------------------------------------------------- harness
def make_scope(method: str, path: str, query: bytes = b"", body: bool = False) -> dict:
    headers = [(b"host", b"bench")]
    if body:
        headers.append((b"content-type", b"application/json"))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 80),
    }


SCENARIOS = [
    ("plain GET", make_scope("GET", "/ping"), False),
    ("path+query GET", make_scope("GET", "/items/abc123", query=b"limit=10"), False),
    ("POST w/ body", make_scope("POST", "/items", body=True), True),
]


async def run_lifespan_startup(app) -> None:
    """Send lifespan.startup and wait for completion (frameworks wire routes here)."""
    started = asyncio.Event()

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        if message["type"] == "lifespan.startup.failed":
            raise RuntimeError(message.get("message", "lifespan startup failed"))
        if message["type"] == "lifespan.startup.complete":
            started.set()

    task = asyncio.ensure_future(app({"type": "lifespan"}, receive, send))
    await started.wait()
    task.cancel()


async def bench_one(app, scope_template: dict, body: bytes | None) -> float:
    """Seconds for N_MEASURE requests (asserts 2xx on every response)."""

    def make_receive():
        sent = False

        async def receive():
            nonlocal sent
            if body is not None and not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        return receive

    async def send(message):
        if message["type"] == "http.response.start":
            assert 200 <= message["status"] < 300, f"unexpected status {message['status']}"

    async def one():
        await app(dict(scope_template), make_receive(), send)

    for _ in range(N_WARMUP):
        await one()
    start = time.perf_counter()
    for _ in range(N_MEASURE):
        await one()
    return time.perf_counter() - start


# ------------------------------------------------------- raw ASGI (honest work)
def build_raw():
    import msgspec

    class Ok(msgspec.Struct):
        ok: bool

    class ItemIn(msgspec.Struct, rename="camel"):
        name: str
        price_cents: int
        tags: list[str]

    class Item(msgspec.Struct, rename="camel"):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    encoder = msgspec.json.Encoder()
    item_in_decoder = msgspec.json.Decoder(ItemIn)

    async def send_json(send, payload: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", b"%d" % len(payload)),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def app(scope, receive, send):
        method, path = scope["method"], scope["path"]
        if method == "GET" and path == "/ping":
            await send_json(send, encoder.encode(Ok(ok=True)))
        elif method == "GET" and path.startswith("/items/"):
            item_id = path[len("/items/") :]
            limit = 20
            for pair in scope["query_string"].decode("latin-1").split("&"):
                key, _, value = pair.partition("=")
                if key == "limit" and value:
                    limit = int(value)
            item = Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])
            await send_json(send, encoder.encode(item))
        elif method == "POST" and path == "/items":
            chunks = []
            while True:
                message = await receive()
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            data = item_in_decoder.decode(b"".join(chunks))
            item = Item(id="new", name=data.name, price_cents=data.price_cents, tags=data.tags)
            await send_json(send, encoder.encode(item))
        else:
            raise AssertionError(f"unrouted request {method} {path}")

    return app


# ------------------------------------------------------------------------ jero
def build_jero():
    from jero import BaseApp, Endpoint, Struct

    class Ok(Struct):
        ok: bool

    class ItemPath(Struct):
        item_id: str

    class PageParams(Struct):
        limit: int = 20

    class ItemIn(Struct, rename="camel"):
        name: str
        price_cents: int
        tags: list[str]

    class Item(Struct, rename="camel"):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    class PingEndpoint(Endpoint, path="/ping"):
        async def get(self) -> Ok:
            return Ok(ok=True)

    class ItemEndpoint(Endpoint, path="/items/{item_id}"):
        async def get(self, path: ItemPath, params: PageParams) -> Item:
            return Item(id=path.item_id, name="gizmo", price_cents=params.limit, tags=["a"])

    class ItemsEndpoint(Endpoint, path="/items"):
        async def post(self, json: ItemIn) -> Item:
            return Item(id="new", name=json.name, price_cents=json.price_cents, tags=json.tags)

    class App(BaseApp):
        async def wire(self) -> None:
            self._include_endpoint(PingEndpoint())
            self._include_endpoint(ItemEndpoint())
            self._include_endpoint(ItemsEndpoint())

    return App()


# -------------------------------------------------------------------- litestar
def build_litestar():
    from dataclasses import dataclass

    from litestar import Litestar, get, post

    @dataclass
    class ItemIn:
        name: str
        price_cents: int
        tags: list[str]

    @dataclass
    class Item:
        id: str
        name: str
        price_cents: int
        tags: list[str]

    @get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @get("/items/{item_id:str}")
    async def get_item(item_id: str, limit: int = 20) -> Item:
        return Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])

    @post("/items")
    async def create_item(data: ItemIn) -> Item:
        return Item(id="new", name=data.name, price_cents=data.price_cents, tags=data.tags)

    return Litestar(route_handlers=[ping, get_item, create_item])


# ------------------------------------------------------------------ blacksheep
def build_blacksheep():
    from dataclasses import dataclass

    import msgspec
    from blacksheep import Application, FromJSON, get, post
    from blacksheep.settings.json import json_settings

    # BlackSheep's documented hook for a faster JSON codec.
    def msgspec_dumps(obj) -> str:
        return msgspec.json.encode(obj).decode("utf-8")

    json_settings.use(loads=msgspec.json.decode, dumps=msgspec_dumps)

    app = Application()

    @dataclass
    class ItemIn:
        name: str
        price_cents: int
        tags: list[str]

    @dataclass
    class Item:
        id: str
        name: str
        price_cents: int
        tags: list[str]

    @get("/ping")
    async def ping():
        return {"ok": True}

    @get("/items/{item_id}")
    async def get_item(item_id: str, limit: int = 20):
        return Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])

    @post("/items")
    async def create_item(item: FromJSON[ItemIn]):
        data = item.value
        return Item(id="new", name=data.name, price_cents=data.price_cents, tags=data.tags)

    return app


# --------------------------------------------------------------------- fastapi
def build_fastapi():
    from fastapi import FastAPI
    from pydantic import BaseModel

    class ItemIn(BaseModel):
        name: str
        price_cents: int
        tags: list[str]

    class Item(BaseModel):
        id: str
        name: str
        price_cents: int
        tags: list[str]

    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    @app.get("/items/{item_id}")
    async def get_item(item_id: str, limit: int = 20) -> Item:
        return Item(id=item_id, name="gizmo", price_cents=limit, tags=["a"])

    @app.post("/items")
    async def create_item(item: ItemIn) -> Item:
        return Item(id="new", name=item.name, price_cents=item.price_cents, tags=item.tags)

    return app


# ------------------------------------------------------------------------ main
async def main() -> None:
    # (label, builder, needs_lifespan, post_body)
    candidates = [
        ("raw (msgspec)", build_raw, False, POST_BODY_CAMEL),
        ("jero", build_jero, True, POST_BODY_CAMEL),
        ("Litestar", build_litestar, True, POST_BODY_SNAKE),
        ("BlackSheep", build_blacksheep, True, POST_BODY_SNAKE),
        ("FastAPI", build_fastapi, True, POST_BODY_SNAKE),
    ]
    results: dict[str, dict[str, float]] = {}
    for name, builder, needs_lifespan, post_body in candidates:
        try:
            app = builder()
        except ImportError:
            print(f"{name}: skipped (not installed)")
            continue
        if needs_lifespan:
            await run_lifespan_startup(app)
        results[name] = {}
        for scenario, scope, has_body in SCENARIOS:
            body = post_body if has_body else None
            results[name][scenario] = min(
                [await bench_one(app, scope, body) for _ in range(BEST_OF)]
            )

    from rich import box
    from rich.console import Console
    from rich.table import Table

    def compact(rps: float) -> str:
        return f"{rps / 1e6:.2f}M" if rps >= 1e6 else f"{rps / 1e3:.0f}k"

    # Multipliers are throughput relative to jero (or, if jero was removed from
    # the run, relative to the first app benchmarked) — jero = 1.00×, like the
    # `vs jero` column in the tables above.
    base = results.get("jero") or next(iter(results.values()))
    table = Table(
        title="Framework overhead per request",
        caption=(
            f"req/s · µs per request · throughput vs jero (higher is faster)\n"
            f"{N_MEASURE:,} requests per cell, best of {BEST_OF}, "
            f"in-process — no server, no sockets"
        ),
        box=box.ROUNDED,
        title_style="bold",
        caption_style="dim",
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("framework", style="cyan", no_wrap=True)
    for scenario, _, _ in SCENARIOS:
        table.add_column(scenario, justify="right", no_wrap=True)
    # Rows ordered by req/s, fastest → slowest, like the tables above.
    for name, cells in sorted(
        results.items(),
        key=lambda item: sum(N_MEASURE / elapsed for elapsed in item[1].values()),
        reverse=True,
    ):
        row = ["[bold white]jero[/]" if name == "jero" else name]
        for scenario, _, _ in SCENARIOS:
            elapsed = cells[scenario]
            us = elapsed / N_MEASURE * 1e6
            mult = base[scenario] / elapsed
            figures = f"{compact(N_MEASURE / elapsed):>5}  {us:>5.1f}µs  {mult:>5.2f}×"
            row.append(f"[bold]{figures}[/]" if name == "jero" else figures)
        table.add_row(*row)
    # A fixed width keeps the table identical in any terminal (or piped to a file).
    Console(width=96).print(table)


asyncio.run(main())
