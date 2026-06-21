"""The opaque request header bag exposed via the ``raw_headers`` binding source."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, repr=False)
class RawHeaders:
    """Immutable, case-insensitive view of the request headers, preserving as-sent
    names and order.

    For forwarding the whole header bag upstream or for diagnostics — *not* for
    reading values you act on (model those in a typed ``headers`` Struct). Lookups
    are case-insensitive (``raw["X-Trace-Id"] == raw["x-traceid"]``); iteration,
    ``items`` and ``repr`` keep the casing as sent. Registers as
    ``Mapping[str, str]`` so it drops straight into ``niquests(headers=...)``; pass
    :meth:`multi_items` instead when repeated headers must survive.
    """

    _pairs: list[tuple[str, str]]  # decoded, original casing, in order

    def _unique(self) -> list[tuple[str, str]]:
        """First-seen pair for each name, compared case-insensitively (Mapping contract)."""
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for name, value in self._pairs:
            lower = name.lower()
            if lower not in seen:
                seen.add(lower)
                out.append((name, value))
        return out

    def __getitem__(self, key: str) -> str:
        lower = key.lower()
        for name, value in self._pairs:
            if name.lower() == lower:
                return value
        raise KeyError(key)

    def get(self, key: str, default: str | None = None) -> str | None:
        """The first value for ``key`` (case-insensitive), or ``default`` if absent."""
        try:
            return self[key]
        except KeyError:
            return default

    def getlist(self, key: str) -> list[str]:
        """Every value sent under ``key`` (case-insensitive), in order."""
        lower = key.lower()
        return [value for name, value in self._pairs if name.lower() == lower]

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        lower = key.lower()
        return any(name.lower() == lower for name, _ in self._pairs)

    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._unique())

    def keys(self) -> list[str]:
        """Unique header names, first-seen casing."""
        return [name for name, _ in self._unique()]

    def values(self) -> list[str]:
        """The value of the first occurrence of each unique header name."""
        return [value for _, value in self._unique()]

    def items(self) -> list[tuple[str, str]]:
        """First-seen ``(name, value)`` pair per unique header name (Mapping contract)."""
        return self._unique()

    def multi_items(self) -> list[tuple[str, str]]:
        """Every header pair, repeats included — use for faithful forwarding."""
        return list(self._pairs)

    def __len__(self) -> int:
        return len(self._unique())

    def __repr__(self) -> str:
        return f"RawHeaders({self._pairs!r})"


Mapping.register(RawHeaders)  # pyrefly: ignore[missing-attribute]  #  pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
