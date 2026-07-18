"""Repo-root pytest configuration.

`isolate`-marked tests run in `pytest-isolate` subprocesses and are incompatible
with xdist, so they are excluded by default via ``-m 'not isolate'`` in
``[tool.pytest.ini_options]``. This reminds the user how to run them — but only
when some were actually excluded.
"""

from __future__ import annotations

import pytest

_isolate_deselected = 0


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Count isolate-marked tests before the ``-m`` marker filter deselects them."""
    global _isolate_deselected
    if "not isolate" not in (config.getoption("markexpr") or ""):
        return
    _isolate_deselected = sum(1 for item in items if item.get_closest_marker("isolate"))


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """When isolate tests were excluded by default, print the command to run them."""
    if _isolate_deselected == 0:
        return
    terminalreporter.write_sep("-", "isolate tests were not run", yellow=True)
    terminalreporter.write_line(
        f"{_isolate_deselected} test(s) marked `isolate` run in subprocesses and are excluded "
        "by default (incompatible with xdist). Run them separately:"
    )
    terminalreporter.write_line("    uv run pytest -m isolate")
