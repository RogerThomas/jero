"""Every complete jero example in the docs must exec, wire, and start.

Every ```python fence that imports jero is enforced by default — the docs rule is that
such examples are full, runnable apps. A fence that is deliberately a partial
illustration (not meant to run on its own) opts out explicitly with a
``# doc-example: fragment`` marker as its first line, so the escape is a decision
someone made and can grep for, not a silent side effect of how a class or variable
happened to be named. This test enforces the rule mechanically: a broken, fragment-
drifted, or wrongly-escaped example fails here, not in a reader's terminal.
"""

import re
from pathlib import Path
from typing import Any, cast

import pytest

from jero import BaseApp
from jero.testing import TestClient

_FRAGMENT_MARKER = "# doc-example: fragment"


def _example_params() -> list[object]:
    docs_dir = Path(__file__).resolve().parents[1] / "docs"
    params: list[object] = []
    for page in sorted(docs_dir.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for index, block in enumerate(re.findall(r"```python\n(.*?)```", text, re.DOTALL)):
            if "from jero" not in block or block.startswith(_FRAGMENT_MARKER):
                continue
            params.append(pytest.param(block, id=f"{page.relative_to(docs_dir)}[{index}]"))
    return params


@pytest.mark.parametrize("block", _example_params())
def test_doc_example_wires_and_starts(block: str) -> None:
    """The example executes as written, and its app wires and starts cleanly."""
    namespace: dict[str, object] = {}
    # exec is the point here: the example must run exactly as the reader sees it.
    exec(compile(block, "<doc-example>", "exec"), namespace)  # noqa: S102 # pylint: disable=exec-used
    app = namespace["app"]
    assert isinstance(app, BaseApp)
    with TestClient(cast(BaseApp[Any], app)):
        pass
