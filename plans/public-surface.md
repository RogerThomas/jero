# Plan: One naming rule for the extension surface (`_` for what you call, `__` for jero's)

Status: **built (2026-07-29), all stages green.** Pure rename/restructure — no
behaviour change, no new concepts. Breaking for every existing app, which is free at 0.0.x.
Build in the staged order at the bottom.

## Goal

Make a name's spelling tell you who may call it, everywhere, with one rule:

- **public** — called from *outside* the class (or the hook the framework calls on you)
- **`_name`** — *you* call it, from inside your own subclass
- **`__name`** — jero's own; you can neither call nor override it

Today the second category is spelled public, on the reasoning that a leading `_` reads as
"keep out" for the API you're meant to use (`docs/guide/wiring.md` has a box saying exactly
that). The cost of that choice is that the underscore stops encoding anything in the place a
reader most wants it — inside a subclass body, where three different call contracts appear in
two spellings:

| | who may call | spelled today |
| :-- | :-- | :-- |
| `enter` / `aenter` / `include_*` / `factory` | inside the subclass only | public |
| your `_create_sub_thing` | inside the subclass only | private |
| your `create_main_thing` | the app's `wire` | public |

Rows 1 and 2 have identical contracts and opposite spellings.

### Why this isn't cosmetic

The wart is **structural, not stylistic**: whenever two services share one
lifecycle-owning dependency, the entering *must* happen in a shared private helper, so a
private method is forced to call a public one.

```python
class MyFactory(BaseFactory):
    @cached_property
    def _sub_thing(self) -> SubThing:
        return self.enter(SubThing())       # private -> public: the direction that reads wrong

    def create_service_a(self) -> ServiceA:
        return ServiceA(self._sub_thing)

    def create_service_b(self) -> ServiceB:
        return ServiceB(self._sub_thing)
```

Hoisting the `enter` up into both `create_*` methods does not fix it — that builds the
sub-thing twice. Lifecycle acquisition follows the *dependency graph*, so it can happen at
any depth, which is what makes `enter`/`aenter` different from `include_*` (called once per
route from the top of `wire`, public→public).

Public→private is the ordinary direction; private→public is the one that jars. Underscoring
the helper fixes both directions at once.

## Decisions

DECIDED: **`_enter` / `_aenter`.** The two members whose call sites are unconstrained.

DECIDED: **the whole extension surface goes `_`, not just those two.** Half-underscored is
worse than either end — `self._enter(...)` next to `self.include_resource(...)` is *less*
consistent than today. Since every one of these names churns anyway, fix the drift in the
same pass (below).

DECIDED: **`wire` stays public.** It is not something you call; it is the abstractmethod the
framework calls on you, so it belongs to the declared interface. `_wire` as a required
override would read like overriding an internal. `__init__` and `__call__` (ASGI entry) are
public for the same reason — outside callers.

DECIDED: **`add_exception_handler` → `_include_exception_handler`.** `add_` is a Starlette
echo with no principle behind it; `include_error_adapter` registers a non-route too and is
already `include_`. Two verbs survive, each with a rule: `_include_*` registers something
with the app, `_create_*` builds and returns a lifecycle-bound object.

DECIDED: **`__`-mangle jero's same-class internals.** `__x` mangles to `_ClassName__x`, so it
is genuinely inaccessible from outside — Pylance still completes `obj._x`, it cannot complete
`obj.__x`. After this, `self.` inside `wire` completes to the extension surface and nothing
else.

DECIDED: **delete `_StackScope`; each class owns its own stack pair.** Mangling is per
defining class, so a base that *declares* `__stack` while two subclasses *assign* it silently
creates three different attributes. Rather than leave the pair at single-underscore, drop the
shared base and give `BaseApp` and `BaseFactory` their own four-line helpers. It has only
those two subclasses and no other references. Bonus: the shared docstring currently reads
*"closed at the app's shutdown (a `BaseFactory` borrows the app's stacks, so resources it
enters share the app's lifetime)"* — a parenthetical that exists only because one method
serves two owners. Split, each states its own truth.

REJECTED: **offering both `enter` and `_enter`.** It contradicts the goal it would serve:
`__`-mangling works *because* editors hide/deprioritise underscored members, while a public
alias exists *because* they show them. It also doubles the surface it is meant to shrink
(`self.` completing both), leaves a permanent "which is blessed?" question, and buys
back-compat that 0.0.x does not need.

REJECTED: **status quo.** Defensible until the shared-dependency case above, which forces
the mixing rather than inviting it.

## The mapping

### `BaseApp`

| | member |
| :-- | :-- |
| public | `wire`, `__init__`, `__call__` |
| `_` | `_factory`, `_enter`, `_aenter`, `_include_resource`, `_include_endpoint`, `_include_openapi`, `_include_error_adapter`, `_include_exception_handler`, `_create_background_tasks` |
| `__` | `__decoder`, `__resolve_factory_type`, `__make_factory`, `__register`, `__check_user_source`, `__include`, `__build_openapi_document`, `__resolve_dynamic`, `__allowed_methods`, `__allow_for`, `__log_openapi_docs`, `__finalize`, `__close_resources`, `__handle_lifespan`, and all state (`__static`, `__dynamic`, `__allowed`, `__allow_cache`, `__decoders`, `__operations`, `__openapi`, `__reverser`, `__exceptions`, `__stack`, `__astack`, `__factory`) |

The `factory` property becomes `_factory` while the **constructor keyword stays `factory=`**
— `DemoApp(factory=mock)` is an outside caller, which is how tests inject. Same word, two
spellings, both correct under the rule.

### `BaseFactory`

| | member |
| :-- | :-- |
| public | `open()` (scripts, notebooks, `FactoryHarness` — all outside callers), and your own `create_*` (the *app's* `wire` calls them, from outside the factory class) |
| `_` | `_enter`, `_aenter` |
| `__` | `__stack`, `__astack` |

### Target shape

```python
class BaseFactory:
    def __init__(self, es: ExitStack, aes: AsyncExitStack) -> None:
        self.__stack = es
        self.__astack = aes

    def _enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager. The factory borrows the app's stacks, so it is
        closed when the app shuts down."""
        return self.__stack.enter_context(cm)

    async def _aenter[T](self, cm: AbstractAsyncContextManager[T]) -> T: ...


class BaseApp[FactoryT = None](ABC):
    def __init__(self, *, factory: FactoryT | None = None) -> None:
        self.__stack = ExitStack()
        self.__astack = AsyncExitStack()
        ...
        self.__factory = factory if factory is not None else self.__make_factory()

    def _enter[T](self, cm: AbstractContextManager[T]) -> T:
        """Open a sync context manager, closed at shutdown in reverse order."""
        return self.__stack.enter_context(cm)
```

```python
class MyApp(BaseApp[MyFactory]):
    async def wire(self) -> None:
        service = await self._factory.create_widget_service()
        tasks = await self._create_background_tasks(drain_timeout=1.0)
        tasks.register(service.process)
        self._include_resource(WidgetResource(service, tasks))
        self._include_openapi(title="Widgets", version="1.0.0")
```

## Invariants that must still hold

- **One shared stack pair.** The app creates the two stacks and passes *those objects* into
  the factory's `__init__` (`_instantiate_factory` matches `es` / `aes` by name), so sharing
  survives the loss of the common base — it was never the base that shared them. Verify
  *behaviourally*: a context manager entered inside a factory `create_*` is closed when the
  app's lifespan ends. (Not by identity — `app._BaseApp__stack is
  factory._BaseFactory__stack` pins mangled privates, which `testing-approach.md` rules out,
  hard-codes both class names into the assertion, and proves the current implementation
  rather than the property that matters: factory resources share the app's lifetime and
  unwind in its single teardown.)
- **One global teardown.** Everything the factory opened and everything `wire` opened unwind
  in one reverse-order pass, async stack first, even when `wire` fails partway.
- `_instantiate_factory` stays a module-level function taking the stacks explicitly, so it
  needs no access to either class's privates.
- Nothing outside `BaseApp` touches its internals: `jero/testing.py` drives the app purely as
  an ASGI callable (verified — only `self._app(scope, receive, send)` and lifespan messages).
- `Resource.METHODS` / `Endpoint.METHODS` / `path` / `ref` / `meta*` stay public: `BaseApp`
  reads them cross-class.

## Call sites to sweep

| member | `.py` | `.md` |
| :-- | --: | --: |
| `include_endpoint` | 104 | 27 |
| `include_openapi` | 50 | 14 |
| `include_resource` | 18 | 30 |
| `add_exception_handler` | 19 | 4 |
| `include_error_adapter` | 16 | 2 |
| `.factory` | 15 | 8 |
| `self.aenter` | 8 | 6 |
| `self.enter` / `create_background_tasks` | 3 / 3 | 2 / 3 |

Docs: `wiring.md` (**delete** the "Why `include_resource`, not `_include_resource`?" box —
there is no dilemma left to explain, just the three-line rule), `complete-example.md`,
`background-tasks.md`, `errors.md` (handler rename), `responses.md`, `testing.md`,
`openapi.md`, `philosophy.md`, `getting-started.md`, `index.md`, `README.md`, `AGENTS.md`
(the "Do **not** re-underscore the extension surface" rule inverts).

## Staged build order

Each stage ends green on `uv run prek run -a`, `task test PYTEST_PROFILE=agent`, and
`task typecheck-public` — the rename is mechanical, so a red gate means a missed call site.

1. **Stacks.** Delete `_StackScope`; give `BaseApp` and `BaseFactory` their own
   `__stack`/`__astack` plus `_enter`/`_aenter`, with per-class docstrings. Update
   `__make_factory` and shutdown to the mangled names. Sweep `demo_app/factory.py`,
   `tests/test_factory_harness.py`. Prove the shared-stack invariant behaviourally in a test
   (a factory-entered context manager closes when the app's lifespan ends).
2. **Extension surface.** `include_*` → `_include_*`, `add_exception_handler` →
   `_include_exception_handler`, `create_background_tasks` → `_create_background_tasks`,
   `factory` property → `_factory` (keep the `factory=` kwarg). Sweep `demo_app`, `tests`.
3. **Mangle internals.** `BaseApp`'s same-class methods and state per the table. Confirm
   `dir(app)` shows no `_Base*__*` leakage into anything users touch, and that a subclass
   overriding a mangled name is inert rather than surprising.
4. **Docs.** The sweep above, `wiring.md` box deleted and replaced with the rule, `AGENTS.md`
   rule inverted, `README.md` example updated. Re-run every complete app in the guides (the
   `exec` harness used for `auth.md`) to prove they still boot.

## OPEN

- **Sharing an async-built dependency has no blessed shape.** `@cached_property` cannot hold
  an awaited value, so two services sharing one async sub-thing need a hand-rolled memo
  (`_sub_thing: SubThing | None = None` plus an `if is None` guard in an async getter). This
  plan does not address it, and it is the uglier half of the motivating example. Candidates:
  an `acached_property`-style descriptor on `BaseFactory`, or a documented idiom. Decide
  separately.
- Whether `Resource`/`Endpoint` gain anything from the same treatment. They carry almost no
  privates, so the current answer is no.
