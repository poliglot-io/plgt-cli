"""Parameterised regression test for the broken-matrix fixture corpus.

Each fixture under ``test/fixtures/broken/<CODE>/`` is a minimal matrix
project crafted so that exactly one ``PLGT_<CODE>`` diagnostic fires when
``validate_project`` runs over it. The test copies each fixture into a
``tmp_path``, scaffolds a synthetic engine cache + lockfile around it, runs
the pipeline, and asserts the expected code appears.

Adding a new fixture only requires creating a directory under
``fixtures/broken/`` and appending its code to ``BROKEN_CODES`` below — no
test code changes needed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from plgt.services.deps_install_service import (
    cache_dir_for,
    lockfile_path_for,
)
from plgt.services.deps_lockfile import (
    LockedPackage,
    Lockfile,
    write_lockfile,
)
from plgt.services.diagnostics import Severity
from plgt.services.validation_pipeline import validate_project

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "broken"

# Codes whose fixture exists under FIXTURES_DIR. Extend this list when a new
# fixture lands; the test is otherwise zero-config.
BROKEN_CODES = [
    "E0001",
    "E0103",
    "E0104",
    "E0201",
    "E0202",
    "E0203",
    "E0401",
    "E0601",
    "E0702",
    "E0800",
]


def _seed_engine_cache(project_dir: Path) -> None:
    """Lay down a minimal poliglot/plgt cache + lockfile so the pipeline's
    preconditions pass. Validation against an empty system matrix surfaces
    most user-error diagnostics cleanly — we don't need real spec content
    to test that, e.g., an unknown predicate is flagged.
    """
    engine = LockedPackage(
        publisher="poliglot",
        name="os",
        version="1.0.0",
        checksum="sha256:engine",
        root=True,
    )
    engine_dir = cache_dir_for(
        project_dir, engine.publisher, engine.name, engine.version
    )
    engine_dir.mkdir(parents=True)
    (engine_dir / "matrix.ttl").write_text(
        "@prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .\n"
        "<https://poliglot.io/plgt/system> a plgt-mtx:Matrix .\n"
    )
    write_lockfile(
        lockfile_path_for(project_dir, workspace=None),
        Lockfile(engine=engine, dependencies=[]),
    )


@pytest.mark.parametrize("code", BROKEN_CODES)
def test_broken_fixture_fires_expected_code(code: str, tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / code
    assert fixture.is_dir(), f"fixture {fixture} missing"

    # Copy the fixture's contents (poliglot.yml + spec/) into tmp_path so the
    # synthetic engine cache lives alongside it without polluting the repo.
    for entry in fixture.iterdir():
        target = tmp_path / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)

    _seed_engine_cache(tmp_path)

    result = validate_project(tmp_path)
    emitted = {d.code for d in result.diagnostics.sorted()}
    expected = f"PLGT_{code}"
    assert expected in emitted, (
        f"fixture broken/{code} should fire {expected}; got {sorted(emitted)}"
    )
    # The expected diagnostic should be an error (severity matters: warnings
    # don't block builds).
    matching = [d for d in result.diagnostics.sorted() if d.code == expected]
    assert matching, f"no diagnostics matched {expected}"
    assert matching[0].severity == Severity.ERROR, (
        f"{expected} should have severity=error; got {matching[0].severity}"
    )
