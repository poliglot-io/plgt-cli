"""Unit tests for ``plgt.services.diagnostics``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from plgt.services.diagnostics import (
    Diagnostic,
    DiagnosticBag,
    Severity,
    diagnostics_as_jsonl,
    diagnostics_as_text,
    group_diagnostics,
    relative_path,
    render_diagnostics_grouped,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestDiagnosticBag:
    def test_error_and_warning_helpers_populate_bag(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0001", "boom", path="foo.ttl", line=3, col=12)
        bag.warning("PLGT_W0001", "tame")
        assert len(bag.diagnostics) == 2
        assert bag.has_errors()

    def test_no_errors_means_no_error_state(self) -> None:
        bag = DiagnosticBag()
        bag.warning("PLGT_W0001", "tame")
        assert not bag.has_errors()

    def test_sorted_orders_errors_first_then_by_code_path_line(self) -> None:
        bag = DiagnosticBag()
        bag.warning("PLGT_W0001", "w1")
        bag.error("PLGT_E0500", "e500a", path="b.ttl")
        bag.error("PLGT_E0001", "e1", path="a.ttl", line=10)
        bag.error("PLGT_E0500", "e500b", path="a.ttl")
        ordered = bag.sorted()
        # All errors before warnings
        assert ordered[0].severity == Severity.ERROR
        assert ordered[3].severity == Severity.WARNING
        # Errors ordered by code, then by path
        assert ordered[0].code == "PLGT_E0001"
        assert ordered[1].code == "PLGT_E0500"
        assert ordered[1].path == "a.ttl"
        assert ordered[2].code == "PLGT_E0500"
        assert ordered[2].path == "b.ttl"


class TestSerialization:
    def test_to_dict_omits_unset_optional_fields(self) -> None:
        d = Diagnostic(
            severity=Severity.ERROR,
            code="PLGT_E0001",
            message="oops",
        )
        out = d.to_dict()
        assert out == {
            "severity": "error",
            "code": "PLGT_E0001",
            "message": "oops",
        }

    def test_to_dict_includes_optional_fields_when_set(self) -> None:
        d = Diagnostic(
            severity=Severity.ERROR,
            code="PLGT_E0204",
            message="Unknown predicate",
            path="spec/foo.rq",
            line=14,
            col=24,
            subject="https://example.com/spec#Foo",
            suggest="wgt-iss:hasState",
            defined_in="spec/ontology.ttl:67",
        )
        out = d.to_dict()
        assert out["path"] == "spec/foo.rq"
        assert out["subject"] == "https://example.com/spec#Foo"
        assert out["suggest"] == "wgt-iss:hasState"
        assert out["defined_in"] == "spec/ontology.ttl:67"

    def test_jsonl_emits_one_diagnostic_per_line(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0001", "a")
        bag.error("PLGT_E0002", "b")
        text = diagnostics_as_jsonl(bag.sorted())
        lines = text.splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["code"] == "PLGT_E0001"
        assert json.loads(lines[1])["code"] == "PLGT_E0002"

    def test_text_includes_location_when_present(self) -> None:
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0001",
            "boom",
            path="foo.ttl",
            line=3,
            col=12,
            suggest="bar",
        )
        text = diagnostics_as_text(bag.sorted())
        assert "foo.ttl:3:12" in text
        assert "did you mean: bar" in text


class TestGroupDiagnostics:
    """``group_diagnostics`` buckets findings by ``(path, subject)`` and orders
    groups so error-bearing resources surface first. The CLI uses this layout
    both for terminal output and for the overflow report.
    """

    def test_groups_diagnostics_by_path_and_subject(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "shape", path="a.ttl", subject="ex:A")
        bag.error("PLGT_E0501", "another", path="a.ttl", subject="ex:A")
        bag.error("PLGT_E0500", "shape", path="a.ttl", subject="ex:B")
        bag.error("PLGT_E0001", "parse", path="b.ttl")  # no subject → file-level
        grouped = group_diagnostics(bag.sorted())
        keys = [(g.path, g.subject) for g in grouped.groups]
        # 3 distinct buckets in a.ttl + 1 in b.ttl.
        assert sorted(keys) == [
            ("a.ttl", "ex:A"),
            ("a.ttl", "ex:B"),
            ("b.ttl", None),
        ]
        # a.ttl / ex:A has the two findings, kept in original order.
        a_a = next(g for g in grouped.groups if g.subject == "ex:A")
        assert [d.code for d in a_a.diagnostics] == ["PLGT_E0500", "PLGT_E0501"]

    def test_error_groups_outrank_warning_groups(self) -> None:
        bag = DiagnosticBag()
        bag.warning("PLGT_W0500", "warn", path="a.ttl", subject="ex:A")
        bag.error("PLGT_E0500", "boom", path="z.ttl", subject="ex:Z")
        grouped = group_diagnostics(bag.sorted())
        # Even though "a.ttl" < "z.ttl" lexically, the error-bearing group
        # comes first because it has a worse severity.
        assert grouped.groups[0].path == "z.ttl"
        assert grouped.groups[1].path == "a.ttl"

    def test_graph_level_group_sinks_to_bottom(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "graph", subject=None, path=None)
        bag.error("PLGT_E0500", "file", path="a.ttl", subject="ex:A")
        grouped = group_diagnostics(bag.sorted())
        assert grouped.groups[0].path == "a.ttl"
        assert grouped.groups[-1].path is None

    def test_file_clusters_keep_their_warnings_with_their_errors(self) -> None:
        """Within a file, error and warning resources stay clustered — a
        warning resource in file A must NOT get sorted after another file's
        error resource. The grouping order is "worst severity in the file"
        first, then within the file by subject severity.
        """
        bag = DiagnosticBag()
        # File A: one error resource + one warning resource.
        bag.error("PLGT_E0500", "e", path="a.ttl", subject="ex:A1")
        bag.warning("PLGT_W0500", "w", path="a.ttl", subject="ex:A2")
        # File B: one error resource.
        bag.error("PLGT_E0500", "e", path="b.ttl", subject="ex:B1")
        grouped = group_diagnostics(bag.sorted())
        # A.error → A.warning → B.error (A's warning stays with A; it is NOT
        # demoted to the bottom after B's error).
        assert [g.path for g in grouped.groups] == ["a.ttl", "a.ttl", "b.ttl"]
        assert grouped.groups[0].subject == "ex:A1"
        assert grouped.groups[1].subject == "ex:A2"
        assert grouped.groups[2].subject == "ex:B1"

    def test_within_group_orders_by_line_then_code(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "later", path="a.ttl", subject="ex:A", line=50)
        bag.error("PLGT_E0501", "earlier", path="a.ttl", subject="ex:A", line=5)
        bag.error("PLGT_E0500", "same-line", path="a.ttl", subject="ex:A", line=5)
        grouped = group_diagnostics(bag.sorted())
        group = grouped.groups[0]
        # Line 5 comes before line 50; within line 5, code PLGT_E0500 < PLGT_E0501.
        assert [(d.line, d.code) for d in group.diagnostics] == [
            (5, "PLGT_E0500"),
            (5, "PLGT_E0501"),
            (50, "PLGT_E0500"),
        ]

    def test_totals_account_for_every_finding(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "a", path="a.ttl", subject="ex:A")
        bag.error("PLGT_E0500", "b", path="a.ttl", subject="ex:A")
        bag.warning("PLGT_W0500", "c", path="a.ttl", subject="ex:B")
        grouped = group_diagnostics(bag.sorted())
        assert grouped.total_resources == 2
        assert grouped.total_findings == 3

    def test_split_returns_shown_and_omitted(self) -> None:
        bag = DiagnosticBag()
        for i in range(5):
            bag.error("PLGT_E0500", f"f{i}", path="a.ttl", subject=f"ex:R{i}")
        grouped = group_diagnostics(bag.sorted())
        shown, omitted = grouped.split(2)
        assert shown.total_resources == 2
        assert omitted.total_resources == 3
        assert shown.total_findings == 2
        assert omitted.total_findings == 3

    def test_split_at_or_above_size_returns_empty_omitted(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "a", path="a.ttl", subject="ex:A")
        grouped = group_diagnostics(bag.sorted())
        shown, omitted = grouped.split(10)
        assert shown.total_resources == 1
        assert omitted.total_resources == 0
        assert omitted.groups == ()


class TestRenderDiagnosticsGrouped:
    def test_renders_path_subject_and_diagnostic_rows(self) -> None:
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "SHACL violation: missing field",
            path="spec/iam.ttl",
            subject="https://example.com/iam#Foo",
            line=23,
        )
        grouped = group_diagnostics(bag.sorted())
        text = render_diagnostics_grouped(grouped)
        assert "spec/iam.ttl" in text
        assert "<https://example.com/iam#Foo>" in text
        assert "error" in text
        assert "PLGT_E0500" in text
        assert "L23" in text
        assert "SHACL violation: missing field" in text

    def test_file_level_group_uses_no_subject_placeholder(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0001", "parse failed", path="spec/foo.ttl")
        text = render_diagnostics_grouped(group_diagnostics(bag.sorted()))
        assert "spec/foo.ttl" in text
        assert "(no subject)" in text

    def test_graph_level_group_uses_pseudo_path(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "graph-level")
        text = render_diagnostics_grouped(group_diagnostics(bag.sorted()))
        assert "(graph-level findings)" in text

    def test_file_header_renders_once_for_multiple_subjects(self) -> None:
        """The grouping unit is ``(path, subject)``, but the *rendering* unit
        is the file — a file with three offending subjects should print the
        path once and nest the subjects beneath it.
        """
        bag = DiagnosticBag()
        bag.error("PLGT_E0500", "a", path="spec/iam.ttl", subject="ex:A")
        bag.error("PLGT_E0500", "b", path="spec/iam.ttl", subject="ex:B")
        bag.error("PLGT_E0500", "c", path="spec/iam.ttl", subject="ex:C")
        text = render_diagnostics_grouped(group_diagnostics(bag.sorted()))
        # Path appears exactly once, even though three resources need to nest under it.
        assert text.count("spec/iam.ttl") == 1
        for subj in ("<ex:A>", "<ex:B>", "<ex:C>"):
            assert subj in text

    def test_format_subject_callable_renders_qnames(self) -> None:
        """The renderer delegates subject formatting so the CLI can wire
        rdflib's namespace manager in and get ``prefix:local`` form.
        """
        bag = DiagnosticBag()
        bag.error(
            "PLGT_E0500",
            "boom",
            path="spec/iam.ttl",
            subject="https://example.com/iam#AdminPolicy",
        )

        def fmt(subject: str | None) -> str:
            if subject == "https://example.com/iam#AdminPolicy":
                return "iam:AdminPolicy"
            return f"<{subject}>" if subject else "(no subject)"

        text = render_diagnostics_grouped(
            group_diagnostics(bag.sorted()), format_subject=fmt
        )
        # Qname-rendered subject is present; the expanded URI is not.
        assert "iam:AdminPolicy" in text
        assert "https://example.com/iam#AdminPolicy" not in text

    def test_format_subject_falls_back_to_angle_brackets_for_none(self) -> None:
        bag = DiagnosticBag()
        bag.error("PLGT_E0001", "parse failed", path="spec/foo.ttl")
        text = render_diagnostics_grouped(group_diagnostics(bag.sorted()))
        # Default formatter renders ``None`` subjects as a friendly placeholder.
        assert "(no subject)" in text


class TestRelativePath:
    def test_returns_relative_when_under_project(self, tmp_path: Path) -> None:
        nested = tmp_path / "spec" / "foo.ttl"
        nested.parent.mkdir(parents=True)
        nested.write_text("")
        assert relative_path(nested, tmp_path) == "spec/foo.ttl"

    def test_falls_back_to_absolute_when_unrelated(self, tmp_path: Path) -> None:
        outside = tmp_path.parent.parent / "somewhere"
        result = relative_path(outside, tmp_path)
        assert "somewhere" in result
