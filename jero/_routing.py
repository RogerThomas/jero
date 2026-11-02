"""Path templates and the compiled patterns the router matches templated paths against.

The sender-free half of routing, split out of ``core`` (pylint's 5000-line module cap) the way
``_assets`` is: parsing a mount path into segments, splitting off an Endpoint's ``:verb``
custom-method suffix, and the per-(method, segment-count) bucket patterns with their ordering.
The request-time resolvers stay in ``core``, inlined on the hot path. A leaf depending only on
``_wiring_types``.
"""

from dataclasses import dataclass, replace

from jero._wiring_types import WiringError

# A template segment: (is_param, static value or slot name).
type Segment = tuple[bool, str]


def path_parts(path: str) -> list[str]:
    """Validate and split a mount path shared by :func:`parse_template` and
    :func:`literal_segments` — the one rule ('/'-rooted) both interpretations agree
    on, kept in one place so it can't drift between them."""
    if not path.startswith("/"):
        raise WiringError(f"path {path!r} must start with '/'")
    return path.split("/")


def parse_template(path: str) -> list[Segment]:
    """Parse a mount path like ``/collections/{collection_id}/pokemon``. An Endpoint's
    trailing ``:verb`` is split off first (:func:`split_action`), so a suffix seen here is
    on an earlier segment or on a path that may not carry one."""
    segments: list[Segment] = []
    slots: set[str] = set()
    for raw in path_parts(path):
        if raw.startswith("{") and raw.endswith("}"):
            slot = raw[1:-1]
            if not slot.isidentifier():
                raise WiringError(f"path {path!r}: slot {raw!r} is not a valid identifier")
            if slot in slots:
                raise WiringError(f"path {path!r}: duplicate slot {raw!r}")
            slots.add(slot)
            segments.append((True, slot))
        elif raw.startswith("{") and "}:" in raw:
            raise WiringError(
                f"path {path!r}: segment {raw!r} carries an action suffix, which only an "
                f"Endpoint may, and only on its last segment (`/things/{{thing_id}}:convert`); "
                f"Resource and WebSocket paths take none",
            )
        elif "{" in raw or "}" in raw:
            raise WiringError(f"path {path!r}: malformed segment {raw!r}")
        else:
            segments.append((False, raw))
    return segments


def split_action(path: str) -> tuple[str, str]:
    """An Endpoint path ending in a custom-method suffix, ``/things/{thing_id}:convert``, as
    ``("/things/{thing_id}", ":convert")``; any other path as ``(path, "")``. The suffix
    follows the last-segment slot (the AIP-136 custom-method shape) and is restricted to
    RFC 3986 unreserved characters, so it can never smuggle in a ``/`` or another slot. A
    wholly literal last segment (``/things:batchGet``) needs no split: it is matched as
    written."""
    head, _, last = path.rpartition("/")
    if not last.startswith("{"):
        return path, ""
    slot, separator, verb = last.partition("}:")
    if not separator:
        return path, ""
    if not verb or not verb.isascii() or not all(c.isalnum() or c in "-._~" for c in verb):
        raise WiringError(
            f"path {path!r}: action {verb!r} must be a non-empty run of letters, digits, "
            f"'-', '.', '_' or '~'",
        )
    return f"{head}/{slot}}}", f":{verb}"


def literal_segments(path: str) -> list[Segment]:
    """Split ``path`` into purely literal segments — unlike :func:`parse_template`, a
    ``{...}``-shaped component is never a slot. For routes whose path components come
    from data (a filesystem path under ``_include_assets``), not a developer-authored
    template: a file or directory literally named ``{locale}`` must never become a
    route parameter."""
    return [(False, raw) for raw in path_parts(path)]


def template_str(segments: list[Segment], action: str = "") -> str:
    """The template as written (``/things/{thing_id}:convert``), for errors and the spec."""
    joined = "/".join(f"{{{value}}}" if is_param else value for is_param, value in segments)
    return joined + action


@dataclass(frozen=True, slots=True)
class Pattern[H]:
    """A compiled templated route within one (method, segment-count) bucket. An action route
    carries its ``:verb`` as ``suffix`` (else ``""``): the last segment must end in it, and the
    last slot binds what precedes it."""

    statics: tuple[tuple[int, str], ...]  # (position, required value)
    params: tuple[tuple[int, str], ...]  # (position, slot name)
    handler: H
    suffix: str = ""

    @classmethod
    def compile(cls, segments: list[Segment], handler: H, suffix: str = "") -> "Pattern[H]":
        """The pattern for a template's segments."""
        statics = tuple((i, value) for i, (is_param, value) in enumerate(segments) if not is_param)
        params = tuple((i, value) for i, (is_param, value) in enumerate(segments) if is_param)
        return cls(statics, params, handler, suffix)

    def matches(self, segments: list[str]) -> bool:
        """Whether a request path's segments match: every static equal and, for an action,
        a non-empty slot value before the suffix. The cold-path form (Allow, 405); the
        resolvers in ``core`` inline the same checks."""
        last = segments[-1]
        if self.suffix and (len(last) <= len(self.suffix) or not last.endswith(self.suffix)):
            return False
        return all(segments[i] == value for i, value in self.statics)

    def with_handler(self, handler: H) -> "Pattern[H]":
        """The same pattern serving ``handler`` (a covered route's wrapper)."""
        return replace(self, handler=handler)


def insert_pattern[H](bucket: list[Pattern[H]], pattern: Pattern[H], label: str) -> None:
    """Add ``pattern`` to its bucket, failing wiring if one of the same shape is there. The
    bucket is kept in match order, so registration order never decides: action routes first
    (``/things/x:convert`` reaches the action, not a ``{thing_id}`` route beside it), then
    more literal segments first (``/groups/special/...`` before ``/groups/{group_id}/...``)."""
    if any(p.statics == pattern.statics and p.suffix == pattern.suffix for p in bucket):
        raise WiringError(f"{label} is already registered")
    bucket.append(pattern)
    bucket.sort(key=lambda p: (not p.suffix, -len(p.statics)))
