#!yeet
"""Build raw Markdown docs for agent consumption into the static site."""

import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class AgentDocsPaths:
    """Filesystem paths used while building the raw Markdown docs bundle."""

    root: Path

    @property
    def docs(self) -> Path:
        """Source documentation directory."""
        return self.root / "docs"

    @property
    def site(self) -> Path:
        """Built static site directory."""
        return self.root / "site"

    @property
    def config(self) -> Path:
        """Zensical configuration file containing the docs nav."""
        return self.root / "zensical.toml"

    @property
    def agents(self) -> Path:
        """Destination directory for copied raw Markdown docs."""
        return self.site / "agents"


def _flatten_nav(items: list[object]) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise TypeError(f"nav item must be a table, got {item!r}")
        nav_item = cast("dict[str, object]", item)
        for title, target in nav_item.items():
            if isinstance(target, str):
                docs.append((title, target))
                continue
            if isinstance(target, list):
                docs.extend(_flatten_nav(cast("list[object]", target)))
                continue
            raise TypeError(f"nav target for {title!r} must be a path or list, got {target!r}")
    return docs


def _read_nav(paths: AgentDocsPaths) -> list[tuple[str, str]]:
    config = cast("dict[str, object]", tomllib.loads(paths.config.read_text()))
    project = config["project"]
    if not isinstance(project, dict):
        raise TypeError("project must be a table")
    project_table = cast("dict[str, object]", project)
    nav = project_table["nav"]
    if not isinstance(nav, list):
        raise TypeError("project.nav must be a list")
    return _flatten_nav(cast("list[object]", nav))


def _source_for(paths: AgentDocsPaths, path: str) -> Path:
    source = paths.docs / path
    if not source.is_file():
        raise FileNotFoundError(source)
    return source


def _agent_index(nav: list[tuple[str, str]]) -> str:
    lines = [
        "# jero Agent Docs",
        "",
        "Raw Markdown entrypoints for agents and other text-first readers.",
        "",
        "- [All docs](agents/all.md)",
    ]
    lines.extend(f"- [{title}](agents/{path})" for title, path in nav)
    return "\n".join(lines) + "\n"


def _all_docs(paths: AgentDocsPaths, nav: list[tuple[str, str]]) -> str:
    sections: list[str] = ["# jero Agent Docs", ""]
    for title, path in nav:
        source = _source_for(paths, path)
        sections += [
            "---",
            "",
            f"# {title}",
            "",
            f"Source: `docs/{path}`",
            "",
            source.read_text(),
            "",
        ]
    return "\n".join(sections).rstrip() + "\n"


def main() -> None:
    """Generate the raw Markdown agent docs inside the built static site."""
    paths = AgentDocsPaths(root=Path(__file__).resolve().parents[1])
    nav = _read_nav(paths)
    paths.agents.mkdir(parents=True, exist_ok=True)

    (paths.site / "agents.md").write_text(_agent_index(nav))
    (paths.agents / "all.md").write_text(_all_docs(paths, nav))

    for _, path in nav:
        destination = paths.agents / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_source_for(paths, path), destination)
