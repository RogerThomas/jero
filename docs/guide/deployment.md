# Deployment

jero is the ASGI app, not the server. Production is: pick an ASGI server, put a
reverse proxy in front of it, tell jero its public URL, and configure logging. This
page covers each.

## Running the app

Any ASGI server works. [granian](https://github.com/emmett-framework/granian) is the
recommended default (it's what the [benchmarks](../performance.md) run), and the
`granian` extra installs both in one step:

```bash
uv add "jero[granian]"
granian --interface asgi --workers 4 myapp:app
```

Each worker is a separate process: `wire` runs in every one, so every worker builds
its own services, connection pools, and [background-task](background-tasks.md) queue.
Anything that must be shared across workers (a cache, a queue) belongs in an external
service, not in process memory.

Host, port, worker count, and request timeouts are all server configuration — see
granian's docs. jero adds nothing to configure on the request path.

## Behind a reverse proxy

Reversed URLs ([`Location` / `Link` headers](links-and-location.md)) are **relative**
by default — always RFC-valid and free of proxy-host ambiguity. Behind a reverse proxy
or load balancer you usually want absolute public URLs instead: the client sees a
different scheme/host than your app does, and the proxy may strip a path prefix.
Reversed URLs become absolute when either environment variable is set (read once when
the app is constructed — no code change):

| Variable | Effect |
| --- | --- |
| `JERO_BASE_URL` | A static public origin (e.g. `https://api.example.com`, may include a prefix). Absolute against it, with no header trust — safest when your origin is fixed. |
| `JERO_TRUST_FORWARDED` | Truthy (`1`/`true`/`yes`/`on`). Rebuild the origin **per request** from `X-Forwarded-Proto` / `-Host` / `-Port`, and restore the stripped path with `X-Forwarded-Prefix`. |

They're **mutually exclusive** — setting both is a startup `WiringError` (one source for
the base). With `JERO_TRUST_FORWARDED=1`, a `create` that reverses to
`/widgets/w1` — for a request carrying `X-Forwarded-Proto: https`,
`X-Forwarded-Host: api.example.com`, `X-Forwarded-Prefix: /api` — emits:

```
Location: https://api.example.com/api/widgets/w1
```

(`X-Forwarded-For` is the *client IP* — it never shapes a URL, so it plays no part
here.) Operation, ref, and `from_path(...)` links are rewritten against the base;
`from_url(...)` links are left exactly as you wrote them.

!!! warning "Only trust headers behind a proxy you control"

    Both variables default off, and that matters for `JERO_TRUST_FORWARDED`: honoring
    `X-Forwarded-*` when you are *not* behind a trusted proxy lets any client spoof
    `X-Forwarded-Host` and poison your `Location` URLs. Setting it is your explicit
    statement that everything reaching the app comes through a proxy you control.
    `JERO_BASE_URL` has no such risk — it's a constant you set, never client input.

`JERO_BASE_URL` also makes the startup docs-URL log line
([OpenAPI & docs](openapi.md#the-docs-ui)) a full, clickable URL.

## What belongs in the proxy

Response-body transformation — compression, caching, ETags — is server/proxy work.
jero's [middleware](middleware.md) can answer requests, add headers, and observe; it
never rewrites bodies, because that costs the buffering jero refuses to pay. Configure
gzip/brotli, caching, and TLS at granian or your reverse proxy.

## Graceful shutdown

On lifespan shutdown, jero closes everything you opened through
[`_enter` / `_aenter`](wiring.md#lifecycle-_enter-_aenter) in reverse order — even if
`wire` failed partway. Queued [background tasks](background-tasks.md#shutdown-drain_timeout)
drain best-effort for up to their `drain_timeout`, then anything left is cancelled and
logged; there is no "wait forever", so shutdown can't hang. How long the *server* waits
for in-flight requests before killing the worker is its own grace-period setting.

## Logging

jero logs to the `jero` logger (background-task failures under `jero.background`).
It never configures logging itself — attach handlers like for any library:

```python
import logging

logging.basicConfig(level=logging.INFO)   # or your dictConfig
```

What arrives there: the docs-URL line at startup (`INFO`), errors raised inside a
[stream](streaming.md#disconnect-handling) after the response started (with traceback),
failed or unhandled [background items](background-tasks.md#enqueuing), and dropped
queued work at shutdown. Request access logs are the server's job — granian has its
own.
