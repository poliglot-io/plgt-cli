"""Unit tests for the ``plgt validate`` command.

These exercise the CLI rendering and exit-code contract by stubbing
``validate_project`` so we don't need a real on-disk matrix project. The
pipeline itself is covered by ``test_validation_pipeline.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from plgt.cmd.validate import app
from plgt.services.diagnostics import DiagnosticBag
from plgt.services.validation_pipeline import ValidationResult
from rdflib import Graph
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _stub_result(
    bag: DiagnosticBag, *, namespaces: dict[str, str] | None = None
) -> ValidationResult:
    """Build a stub ValidationResult. When ``namespaces`` is supplied, attach
    a Graph with those prefixes bound so the CLI's qname formatter has a
    namespace table to consult. The graph contains no triples — it's only
    there to carry the prefix bindings, which is all `compute_qname` needs.
    """
    graph: Graph | None = None
    if namespaces:
        graph = Graph()
        for prefix, ns in namespaces.items():
            graph.bind(prefix, ns)
    return ValidationResult(diagnostics=bag, assembled=graph)


def _run_validate(project_dir: Path, *extra_args: str):
    """Helper: invoke `plgt validate` against ``project_dir``."""
    return runner.invoke(app, ["--project-dir", str(project_dir), *extra_args])


class TestExitCode:
    """The exit code contract is severity-aware: only errors fail the run.

    This was the prior author's claim under review — the test locks in the
    correct behavior so a future refactor doesn't accidentally flip it.
    """

    def test_warning_only_run_exits_zero(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.warning("PLGT_W0500", "non-fatal", path="a.ttl", subject="ex:A")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        assert result.exit_code == 0

    def test_info_only_run_exits_zero(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.info("PLGT_I0500", "hint", path="a.ttl", subject="ex:A")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        assert result.exit_code == 0

    def test_error_makes_run_exit_one(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "boom", path="a.ttl", subject="ex:A")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        assert result.exit_code == 1

    def test_mixed_severities_with_error_exits_one(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "boom", path="a.ttl", subject="ex:A")
        bag.warning("PLGT_W0500", "tame", path="b.ttl", subject="ex:B")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        assert result.exit_code == 1


class TestGroupedOutput:
    """Terminal output groups by ``(file, subject)`` and prints a summary."""

    def test_clean_run_announces_clean(self, tmp_path: Path) -> None:
        with patch(
            "plgt.cmd.validate.validate_project",
            return_value=_stub_result(DiagnosticBag()),
        ):
            result = _run_validate(tmp_path)
        assert result.exit_code == 0
        assert "Validation clean" in result.output

    def test_groups_render_path_subject_and_code(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "missing field",
            path="spec/iam.ttl",
            subject="https://example.com/iam#Foo",
            line=23,
        )
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        # File path is the section header; subject is the sub-header; the
        # diagnostic row has the severity, code, and line.
        assert "spec/iam.ttl" in result.output
        assert "https://example.com/iam#Foo" in result.output
        assert "PLGT_E0500" in result.output
        assert "L23" in result.output

    def test_file_header_collapses_for_multiple_subjects(self, tmp_path: Path) -> None:
        """Three offending resources in one file → one file header in the
        terminal output, three subject sub-headers beneath it. The earlier
        renderer printed the path three times.
        """
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "a", path="spec/iam.ttl", subject="ex:A")
        bag.error("PLGT_E0500", "b", path="spec/iam.ttl", subject="ex:B")
        bag.error("PLGT_E0500", "c", path="spec/iam.ttl", subject="ex:C")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        # Strip the summary footer line that also contains "spec" once...
        # actually the footer doesn't mention paths, so a raw count is safe.
        assert result.output.count("spec/iam.ttl") == 1

    def test_subjects_render_as_qnames_when_prefix_bound(self, tmp_path: Path) -> None:
        """When the assembled graph binds a prefix for the subject's
        namespace, the CLI shows the qname form rather than the full IRI.
        """
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "boom",
            path="spec/iam.ttl",
            subject="https://example.com/iam#AdminPolicy",
        )
        with patch(
            "plgt.cmd.validate.validate_project",
            return_value=_stub_result(
                bag, namespaces={"iam": "https://example.com/iam#"}
            ),
        ):
            result = _run_validate(tmp_path)
        assert "iam:AdminPolicy" in result.output
        # The expanded IRI should not also appear — the whole point is to
        # not double-render the resource identifier.
        assert "https://example.com/iam#AdminPolicy" not in result.output

    def test_subjects_fall_back_to_angle_brackets_without_prefix(
        self, tmp_path: Path
    ) -> None:
        """No prefix bound for the namespace → wrap the full IRI in angle
        brackets rather than letting rdflib invent a ``ns1:`` prefix.
        """
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "boom",
            path="spec/iam.ttl",
            subject="https://unmatched.example.com/iam#Foo",
        )
        with patch(
            "plgt.cmd.validate.validate_project",
            return_value=_stub_result(bag, namespaces={}),
        ):
            result = _run_validate(tmp_path)
        assert "<https://unmatched.example.com/iam#Foo>" in result.output

    def test_summary_counts_each_severity(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "e", path="a.ttl", subject="ex:A")
        bag.error("PLGT_E0501", "e2", path="a.ttl", subject="ex:A")
        bag.warning("PLGT_W0500", "w", path="b.ttl", subject="ex:B")
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path)
        assert "2 error" in result.output
        assert "1 warning" in result.output
        assert "2 resource" in result.output


class TestTruncationAndOverflow:
    """``--max-resources`` caps terminal output and dumps the rest to a file."""

    def _bag_with_n_resources(self, n: int) -> DiagnosticBag:
        bag = DiagnosticBag()
        for i in range(n):
            bag.error(
                "PLGT_E0500",
                f"finding {i}",
                path=f"spec/file{i:02d}.ttl",
                subject=f"ex:R{i}",
            )
        return bag

    def test_terminal_truncates_to_max_resources(self, tmp_path: Path) -> None:
        bag = self._bag_with_n_resources(5)
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path, "--max-resources", "2")
        # First two files shown in output, latter three are not.
        assert "spec/file00.ttl" in result.output
        assert "spec/file01.ttl" in result.output
        assert "spec/file02.ttl" not in result.output
        assert "spec/file04.ttl" not in result.output
        # The truncation footer summarizes the omitted slice.
        assert "3 more resource" in result.output

    def test_overflow_file_written_when_truncated(self, tmp_path: Path) -> None:
        bag = self._bag_with_n_resources(5)
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            _run_validate(tmp_path, "--max-resources", "2")
        overflow = tmp_path / ".matrix" / "reports" / "validate.txt"
        assert overflow.exists()
        contents = overflow.read_text()
        # The overflow report has the full, ungroomed list — every file.
        for i in range(5):
            assert f"spec/file{i:02d}.ttl" in contents

    def test_overflow_file_not_written_when_within_budget(self, tmp_path: Path) -> None:
        bag = self._bag_with_n_resources(2)
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            _run_validate(tmp_path, "--max-resources", "10")
        overflow = tmp_path / ".matrix" / "reports" / "validate.txt"
        assert not overflow.exists()

    def test_max_resources_zero_means_unlimited(self, tmp_path: Path) -> None:
        bag = self._bag_with_n_resources(20)
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path, "--max-resources", "0")
        # Every resource printed; no truncation footer.
        for i in range(20):
            assert f"spec/file{i:02d}.ttl" in result.output
        assert "more resource" not in result.output
        overflow = tmp_path / ".matrix" / "reports" / "validate.txt"
        assert not overflow.exists()


class TestJsonOutputUnchanged:
    """``--json`` is the machine-consumption contract: full stream, no
    grouping, no overflow file.
    """

    def test_json_mode_emits_all_diagnostics_and_skips_grouping(
        self, tmp_path: Path
    ) -> None:
        bag = DiagnosticBag()
        for i in range(5):
            bag.error(
                "PLGT_E0500",
                f"f{i}",
                path=f"spec/file{i:02d}.ttl",
                subject=f"ex:R{i}",
            )
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path, "--json", "--max-resources", "2")
        lines = [json.loads(ln) for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 5
        # No truncation footer, no overflow file, even with max-resources=2.
        assert "more resource" not in result.output
        overflow = tmp_path / ".matrix" / "reports" / "validate.txt"
        assert not overflow.exists()

    def test_json_diagnostic_includes_subject_field(self, tmp_path: Path) -> None:
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "boom",
            path="spec/iam.ttl",
            subject="https://example.com/iam#Foo",
        )
        with patch(
            "plgt.cmd.validate.validate_project", return_value=_stub_result(bag)
        ):
            result = _run_validate(tmp_path, "--json")
        line = json.loads(result.output.strip())
        assert line["subject"] == "https://example.com/iam#Foo"
