"""Unit behavior of the RawHeaders opaque header bag (a small pure type)."""

from collections.abc import Mapping

import pytest

from jero import RawHeaders


@pytest.fixture(name="raw")
def _raw() -> RawHeaders:
    """A bag with mixed casing and a repeated header, as a real request might send."""
    return RawHeaders(
        [
            ("X-Trace-Id", "trace"),
            ("Set-Cookie", "first"),
            ("Set-Cookie", "second"),
            ("Content-Type", "application/json"),
        ]
    )


def test_getitem_is_case_insensitive(raw: RawHeaders) -> None:
    """The first value for a name is found regardless of lookup casing."""
    assert raw["X-Trace-Id"] == "trace"
    assert raw["x-trace-id"] == "trace"
    assert raw["X-TRACE-ID"] == "trace"


def test_getitem_missing_raises_keyerror(raw: RawHeaders) -> None:
    """A missing name raises KeyError, like any Mapping."""
    with pytest.raises(KeyError):
        _ = raw["missing"]


def test_get_returns_default_when_absent(raw: RawHeaders) -> None:
    """get falls back to the default for an absent name."""
    assert raw.get("x-trace-id") == "trace"
    assert raw.get("missing") is None
    assert raw.get("missing", "default") == "default"


def test_contains_is_case_insensitive(raw: RawHeaders) -> None:
    """Membership is case-insensitive; non-str keys are simply absent."""
    assert "x-trace-id" in raw
    assert "X-Trace-Id" in raw
    assert "missing" not in raw
    assert 1 not in raw


def test_getlist_returns_all_values_case_insensitive(raw: RawHeaders) -> None:
    """getlist returns every value for a repeated name, in order."""
    assert raw.getlist("set-cookie") == ["first", "second"]
    assert raw.getlist("Set-Cookie") == ["first", "second"]
    assert raw.getlist("x-trace-id") == ["trace"]
    assert raw.getlist("missing") == []


def test_multi_items_preserves_every_pair_and_casing(raw: RawHeaders) -> None:
    """multi_items is the faithful wire view: every pair, repeats and casing intact."""
    assert raw.multi_items() == [
        ("X-Trace-Id", "trace"),
        ("Set-Cookie", "first"),
        ("Set-Cookie", "second"),
        ("Content-Type", "application/json"),
    ]


def test_items_keys_values_are_unique_first_seen_casing(raw: RawHeaders) -> None:
    """The Mapping views collapse repeats to the first-seen pair, keeping its casing."""
    assert raw.keys() == ["X-Trace-Id", "Set-Cookie", "Content-Type"]
    assert raw.values() == ["trace", "first", "application/json"]
    assert raw.items() == [
        ("X-Trace-Id", "trace"),
        ("Set-Cookie", "first"),
        ("Content-Type", "application/json"),
    ]


def test_iter_yields_unique_keys_in_sent_casing(raw: RawHeaders) -> None:
    """Iteration yields unique names (Mapping contract) in their as-sent casing."""
    assert list(raw) == ["X-Trace-Id", "Set-Cookie", "Content-Type"]


def test_len_counts_unique_keys(raw: RawHeaders) -> None:
    """len is the number of unique header names, not the number of pairs."""
    assert len(raw) == 3


def test_repr_shows_as_sent_pairs() -> None:
    """repr exposes the underlying pairs with original casing."""
    assert repr(RawHeaders([("X-A", "value")])) == "RawHeaders([('X-A', 'value')])"


def test_registered_as_mapping(raw: RawHeaders) -> None:
    """RawHeaders is a Mapping, so it drops into niquests(headers=...)."""
    assert isinstance(raw, Mapping)
