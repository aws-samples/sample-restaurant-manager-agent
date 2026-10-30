# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guards for the repo's bandit (security linter) configuration.

WHY THIS CONFIG EXISTS
----------------------
The ProtoShield ``bandit`` plugin runs::

    uv tool run bandit -r -c /project/pyproject.toml --exclude='<vendored dirs>' /project/

It passes this repo's ``pyproject.toml`` with ``-c`` whenever it sees a
``[tool.bandit]`` table, and bandit *appends* that table's ``exclude_dirs`` to
the scanner's own CLI ``--exclude`` rather than replacing it (see
``bandit.core.manager.discover_files``). So ``exclude_dirs`` here is additive:
it narrows the scan on top of the vendored-directory exclusions, and cannot
accidentally re-admit ``node_modules`` / ``cdk.out`` / ``.venv``.

The table excludes TEST code only. Bandit's two loudest checks are correct
behaviour in a test -- B101 (``assert``, which is how pytest expresses every
assertion) and B105 (obvious fixtures such as ``AKIATEST`` / ``tok123``) -- and
before this config they buried everything else: a scan reported 1,124 first-party
findings, 1,104 of them in test files, leaving the 20 findings in *shipped* code
illegible.

WHAT THESE TESTS DO AND DO NOT COVER
------------------------------------
These are offline, structural checks on the config itself: that the table is
present and shaped the way the scanner needs, and -- the part that actually
matters -- that every exclusion stays scoped to test code.

They deliberately do **not** run bandit (that needs the tool installed and is
the scanner's job). The invariant they protect is that nobody widens
``exclude_dirs`` to silence a finding in shipped code, because that is the one
edit here that would quietly reduce coverage while still looking like
"scanner configuration".
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# An exclusion is only acceptable if it can match test code and nothing else.
# Both accepted forms are anchored on a path segment so that, e.g., a directory
# named "latest/" or a module named "protest.py" cannot satisfy them.
_ALLOWED_EXCLUSIONS = frozenset({"*/tests/*", "*/test_*.py"})


@pytest.fixture(scope="module")
def bandit_config() -> dict:
    """The ``[tool.bandit]`` table, as the ProtoShield scanner would read it."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    tool = data.get("tool", {})
    assert "bandit" in tool, (
        "pyproject.toml has no [tool.bandit] table. Without it the ProtoShield "
        "bandit plugin runs config-less and reports ~1,100 B101/B105 findings "
        "from test files, which hides the findings in shipped code."
    )
    return tool["bandit"]


def test_exclude_dirs_is_a_list_of_strings(bandit_config: dict) -> None:
    """bandit reads this via ``get_option``; a scalar would silently misbehave."""
    exclude_dirs = bandit_config.get("exclude_dirs")
    assert isinstance(exclude_dirs, list) and exclude_dirs, (
        "exclude_dirs must be a non-empty list of glob strings; got "
        f"{exclude_dirs!r}"
    )
    assert all(isinstance(pattern, str) for pattern in exclude_dirs), (
        f"every exclude_dirs entry must be a string; got {exclude_dirs!r}"
    )


def test_exclusions_are_scoped_to_test_code(bandit_config: dict) -> None:
    """The load-bearing check: no exclusion may reach shipped code.

    This is deliberately a strict allowlist rather than a "does it look
    test-ish" heuristic. A pattern like ``*/simulator/*`` or a bare ``*`` would
    disable the scanner for real code while still reading as configuration, and
    the resulting drop in findings looks identical to a genuine fix.
    """
    extra = sorted(set(bandit_config["exclude_dirs"]) - _ALLOWED_EXCLUSIONS)
    assert not extra, (
        f"exclude_dirs contains {extra}, which is not scoped to test code. "
        f"Only {sorted(_ALLOWED_EXCLUSIONS)} are allowed here -- bandit must "
        "keep scanning everything that ships. To silence one specific finding "
        "in shipped code, put an inline `# nosec <ID>` with a written reason "
        "next to that line instead, so the justification lives at the code."
    )


def test_no_check_is_globally_disabled(bandit_config: dict) -> None:
    """A global ``skips`` would drop a check everywhere, including shipped code.

    Excluding test *paths* is scoped and reversible; skipping a test *ID* is
    neither, because it applies to code that ships. Per-plugin subtables (e.g.
    ``[tool.bandit.assert_used]``) are equally global in effect, so they are
    rejected too.
    """
    assert not bandit_config.get("skips"), (
        "[tool.bandit] declares `skips`, which disables that check for shipped "
        f"code as well as tests: {bandit_config.get('skips')!r}. Prefer keeping "
        "the check on and excluding test paths, or an inline `# nosec` at the "
        "one line that needs it."
    )
    subtables = sorted(
        key for key, value in bandit_config.items() if isinstance(value, dict)
    )
    assert not subtables, (
        f"[tool.bandit] has per-plugin subtable(s) {subtables}. bandit honours "
        "these on their own, and their effect is global -- use path exclusions "
        "or an inline `# nosec` instead."
    )
