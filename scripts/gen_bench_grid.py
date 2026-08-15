#!yeet
"""Regenerate docs/assets/bench-grid.svg — the benchmark at-a-glance grid.

The data source is an api-benchmarks run folder
(https://github.com/RogerThomas/api-benchmarks, reports/<run_id>/), whose
results.json carries the run configuration and every attempt:

    uv run yeet scripts/gen_bench_grid.py /path/to/reports/<run_id>

The throughput rows (best attempt per framework and test) and the footer's run
configuration all come from results.json, so nothing is transcribed by hand.
After regenerating, update docs/performance.md's tables to the same run's
report — the grid and the tables must never cite different runs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import msgspec

from jero import Struct


class RunConfig(Struct):
    """The slice of results.json's `run` object the grid's footer needs."""

    vus: int
    duration: str
    best_of: int
    python_server: str
    ec2_instance_type: str


class Attempt(Struct):
    """One (framework, test, attempt) row from results.json."""

    framework: str
    test: str
    reqs_per_sec: float
    is_best: bool


class Report(Struct):
    """The results.json document."""

    run: RunConfig
    results: list[Attempt]


class Panel(Struct):
    """One test's ranking: a title, a right-aligned hint, and fastest-first rows."""

    title: str
    hint: str
    rows: list[tuple[str, float]]


def _panels(report: Report) -> list[Panel]:
    meta = [
        ("test1", "JSON — GET /info", "the pure framework path"),
        ("test2", "JWT — POST /movies", "authed write path"),
        ("test3", "Proxy — GET /catalog", "same Rust client under each"),
        ("test4", "Database — GET /users/me", "bound by the DB driver"),
    ]
    best = [attempt for attempt in report.results if attempt.is_best]
    panels: list[Panel] = []
    for test, title, hint in meta:
        rows = sorted(
            (
                (attempt.framework, attempt.reqs_per_sec / 1000)
                for attempt in best
                if attempt.test == test
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        if not rows:
            raise ValueError(f"no best attempts for {test!r} in results.json")
        panels.append(Panel(title=title, hint=hint, rows=rows))
    return panels


def _footer(run: RunConfig) -> str:
    return (
        f"req/s, higher is better · {run.vus} VUs · {run.duration} · best-of-{run.best_of} · "
        f"1 dedicated core · EC2 {run.ec2_instance_type} · Python 3.13 ({run.python_server}+uvloop)"
    )


@dataclass(frozen=True, slots=True)
class GridRenderer:
    """Emits the four-panel benchmark grid as a self-contained SVG document."""

    _panels: list[Panel]
    _footer: str

    _grid_title: ClassVar[str] = "jero — the fastest Python ASGI framework"
    _subtitle: ClassVar[str] = (
        "Throughput (req/s) · each panel scaled to its own fastest · "
        "gin (Go), elysia (Bun), spring-boot (Java) for context"
    )

    _font: ClassVar[str] = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    _ink: ClassVar[str] = "#0b0b0b"
    _ink_secondary: ClassVar[str] = "#52514e"
    _ink_muted: ClassVar[str] = "#898781"
    _jero_blue: ClassVar[str] = "#2a78d6"
    _other_gray: ClassVar[str] = "#a8adb3"
    _surface: ClassVar[str] = "#fcfcfb"
    _border: ClassVar[str] = "rgba(11,11,11,0.10)"

    _width: ClassVar[int] = 800
    _column_x: ClassVar[tuple[int, int]] = (24, 412)
    _column_width: ClassVar[int] = 376
    _label_end: ClassVar[int] = 90  # relative to panel x
    _bar_start: ClassVar[int] = 98  # relative to panel x
    _bar_max: ClassVar[int] = 232  # _column_width - _bar_start - room for the value label
    _bar_height: ClassVar[int] = 14
    _row_pitch: ClassVar[int] = 26
    _bar_radius: ClassVar[int] = 4
    _value_pad: ClassVar[int] = 6

    @staticmethod
    def _fmt(value: float) -> str:
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"

    def _text(
        self,
        x: float,
        y: float,
        content: str,
        size: float,
        fill: str,
        *,
        weight: str = "400",
        anchor: str = "",
        nums: bool = False,
    ) -> str:
        attrs = f'x="{x}" y="{y}" font-size="{size}" fill="{fill}" font-family="{self._font}"'
        if weight != "400":
            attrs += f' font-weight="{weight}"'
        if anchor:
            attrs += f' text-anchor="{anchor}"'
        if nums:
            attrs += ' style="font-variant-numeric:tabular-nums"'
        return f"<text {attrs}>{content}</text>"

    def _swatch(self, x: int, fill: str) -> str:
        return f'<rect x="{x}" y="27" width="10" height="10" rx="2" fill="{fill}"/>'

    def _bar(self, x: float, y: float, width: float, fill: str) -> str:
        radius = self._bar_radius
        width = max(width, 2 * radius)
        right = round(x + width, 2)
        shoulder = round(right - radius, 2)
        bottom = y + self._bar_height
        return (
            f'<path d="M{x},{y} L{shoulder},{y} Q{right},{y} {right},{y + radius} '
            f"L{right},{bottom - radius} Q{right},{bottom} {shoulder},{bottom} "
            f'L{x},{bottom} Z" fill="{fill}"/>'
        )

    def _panel(self, panel: Panel, panel_x: int, title_y: float) -> list[str]:
        parts = [
            self._text(panel_x, title_y, panel.title, 12.5, self._ink, weight="600"),
            self._text(
                panel_x + self._column_width - 21,
                title_y,
                panel.hint,
                10.5,
                self._ink_muted,
                anchor="end",
            ),
        ]
        peak = max(value for _, value in panel.rows)
        for index, (name, value) in enumerate(panel.rows):
            bar_y = title_y + 19 + index * self._row_pitch
            text_y = bar_y + 10.6
            is_jero = name == "jero"
            weight = "700" if is_jero else "400"
            label_ink = self._ink if is_jero else self._ink_secondary
            width = round(value / peak * self._bar_max, 1)
            value_x = round(
                panel_x + self._bar_start + max(width, 2 * self._bar_radius) + self._value_pad, 2
            )
            label_x = panel_x + self._label_end
            bar_fill = self._jero_blue if is_jero else self._other_gray
            value_text = self._fmt(value)
            parts += [
                self._text(label_x, text_y, name, 11, label_ink, weight=weight, anchor="end"),
                self._bar(panel_x + self._bar_start, bar_y, width, bar_fill),
                self._text(value_x, text_y, value_text, 10.5, label_ink, weight=weight, nums=True),
            ]
        return parts

    def render(self) -> str:
        """Return the complete SVG document."""
        panel_height = 19 + len(self._panels[0].rows) * self._row_pitch
        title_y_top = 90.0
        title_y_bottom = title_y_top + panel_height + 42
        footer_y = title_y_bottom + panel_height + 28
        height = int(footer_y + 24)
        width = self._width

        parts = [
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
                f'width="{width}" height="{height}" role="img" aria-label="{self._grid_title}">'
            ),
            (
                f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" '
                f'fill="{self._surface}" stroke="{self._border}"/>'
            ),
            self._text(24, 38, self._grid_title, 16, self._ink, weight="700"),
            self._text(24, 57, self._subtitle, 11.5, self._ink_secondary),
            self._swatch(width - 176, self._jero_blue),
            self._text(width - 162, 36, "jero", 11, self._ink, weight="700"),
            self._swatch(width - 118, self._other_gray),
            self._text(width - 104, 36, "others", 11, self._ink_secondary),
        ]
        positions = [
            (self._column_x[0], title_y_top),
            (self._column_x[1], title_y_top),
            (self._column_x[0], title_y_bottom),
            (self._column_x[1], title_y_bottom),
        ]
        for panel, (panel_x, title_y) in zip(self._panels, positions, strict=True):
            parts += self._panel(panel, panel_x, title_y)
        parts += [self._text(24, footer_y, self._footer, 10.5, self._ink_muted), "</svg>"]
        return "\n".join(parts) + "\n"


def main(run_folder: str) -> None:
    """Regenerate the benchmark grid SVG in docs/assets from a run folder's results.json."""
    report = msgspec.json.decode((Path(run_folder) / "results.json").read_bytes(), type=Report)
    destination = Path(__file__).resolve().parents[1] / "docs" / "assets" / "bench-grid.svg"
    destination.write_text(
        GridRenderer(_panels(report), _footer(report.run)).render(), encoding="utf-8"
    )
    print(f"wrote {destination}")
