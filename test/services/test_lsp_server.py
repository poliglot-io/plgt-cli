"""Unit tests for ``plgt.services.lsp_server``.

Tests the utility helpers (extraction, diagnostic conversion) without
launching a real LSP server. The server-as-a-whole integration is left to
manual editor testing; here we lock in the pure functions agents will
hit when authoring.
"""

from __future__ import annotations

from lsprotocol import types as lsp
from plgt.services.diagnostics import Diagnostic, Severity
from plgt.services.lsp_server import (
    _extract_prefixed_name_at,
    _format_hover,
    _prefix_label_before_cursor,
    _to_lsp_diagnostic,
)
from plgt.services.schema_service import TermDescription

_PREFIX_TABLE = {
    "wgt-iss": "https://example.com/spec/issues#",
    "plgt-act": "https://poliglot.io/os/spec/actions#",
}


class TestExtractPrefixedNameAt:
    def test_returns_full_uri_when_cursor_in_token(self) -> None:
        text = "    plgt-act:Action ;\n"
        result = _extract_prefixed_name_at(
            text,
            lsp.Position(line=0, character=10),  # inside "plgt-act"
            _PREFIX_TABLE,
        )
        assert result == "https://poliglot.io/os/spec/actions#Action"

    def test_returns_none_when_cursor_in_whitespace(self) -> None:
        text = "    plgt-act:Action ;\n"
        result = _extract_prefixed_name_at(
            text,
            lsp.Position(line=0, character=0),
            _PREFIX_TABLE,
        )
        assert result is None

    def test_returns_none_when_prefix_unknown(self) -> None:
        text = "    unknown:Foo ;\n"
        result = _extract_prefixed_name_at(
            text,
            lsp.Position(line=0, character=10),
            _PREFIX_TABLE,
        )
        assert result is None

    def test_handles_local_names_with_hyphens(self) -> None:
        text = "wgt-iss:has-state\n"
        result = _extract_prefixed_name_at(
            text,
            lsp.Position(line=0, character=12),
            _PREFIX_TABLE,
        )
        assert result == "https://example.com/spec/issues#has-state"

    def test_statement_terminator_dot_is_not_swept_into_name(self) -> None:
        # `plgt-act:Action.` (no space before the dot) was being read as the local name
        # "Action." with the trailing dot included. Confirm the dot is now boundary-only.
        text = "plgt-act:Action."
        result = _extract_prefixed_name_at(
            text,
            lsp.Position(line=0, character=10),
            _PREFIX_TABLE,
        )
        assert result == "https://poliglot.io/os/spec/actions#Action"


class TestPrefixLabelBeforeCursor:
    def test_returns_prefix_when_cursor_just_after_colon(self) -> None:
        text = "    plgt-act:"
        result = _prefix_label_before_cursor(text, lsp.Position(line=0, character=13))
        assert result == "plgt-act"

    def test_returns_prefix_when_local_name_partially_typed(self) -> None:
        text = "    plgt-act:Act"
        result = _prefix_label_before_cursor(
            text, lsp.Position(line=0, character=len(text))
        )
        assert result == "plgt-act"

    def test_returns_none_when_no_colon_nearby(self) -> None:
        text = "    just words here"
        result = _prefix_label_before_cursor(
            text, lsp.Position(line=0, character=len(text))
        )
        assert result is None


class TestToLspDiagnostic:
    def test_maps_error_severity(self, tmp_path) -> None:
        d = Diagnostic(
            severity=Severity.ERROR,
            code="PLGT_E0001",
            message="boom",
            path="spec/foo.ttl",
            line=3,
            col=12,
        )
        lsp_d = _to_lsp_diagnostic(d, tmp_path)
        assert lsp_d.severity == lsp.DiagnosticSeverity.Error
        assert lsp_d.code == "PLGT_E0001"
        # LSP uses 0-indexed positions; we emit (line-1, col-1)
        assert lsp_d.range.start.line == 2
        assert lsp_d.range.start.character == 11

    def test_maps_warning_severity(self, tmp_path) -> None:
        d = Diagnostic(
            severity=Severity.WARNING,
            code="PLGT_W0001",
            message="meh",
        )
        lsp_d = _to_lsp_diagnostic(d, tmp_path)
        assert lsp_d.severity == lsp.DiagnosticSeverity.Warning

    def test_includes_suggest_in_message(self, tmp_path) -> None:
        d = Diagnostic(
            severity=Severity.ERROR,
            code="PLGT_E0204",
            message="Unknown predicate wgt-iss:hasStateType",
            suggest="wgt-iss:hasState",
        )
        lsp_d = _to_lsp_diagnostic(d, tmp_path)
        assert "Did you mean: wgt-iss:hasState" in lsp_d.message

    def test_range_spans_token_when_file_readable(self, tmp_path) -> None:
        # When the path resolves to a real file, the diagnostic range should span the
        # actual token at line:col, not a 1-character placeholder.
        spec = tmp_path / "spec"
        spec.mkdir()
        (spec / "foo.ttl").write_text(
            "@prefix ex: <https://example.com#> .\n"
            'ex:hasStateType rdfs:label "x" .\n'  # line 2, col 1 = 'ex:hasStateType'
        )
        d = Diagnostic(
            severity=Severity.ERROR,
            code="PLGT_E0201",
            message="unknown",
            path="spec/foo.ttl",
            line=2,
            col=1,
        )
        lsp_d = _to_lsp_diagnostic(d, tmp_path)
        # 'ex:hasStateType' is 15 chars; the range should span it (start..start+15).
        assert lsp_d.range.start.character == 0
        assert lsp_d.range.end.character == len("ex:hasStateType")


class TestPublishDiagnosticsIntegration:
    """Integration-style: instantiate a real server, drive
    ``_publish_all_diagnostics``, and assert it calls the LSP API correctly.
    Would have caught the pygls 2.x API rename
    (`text_document_publish_diagnostics` vs the old `publish_diagnostics`).
    """

    def test_publish_uses_pygls_v2_api_and_clears_stale(
        self, tmp_path, monkeypatch
    ) -> None:
        from plgt.services import lsp_server as ls_mod

        server = ls_mod.create_server()
        # Skip the pygls workspace plumbing: stub the resolver helper to
        # return our tmp_path directly.
        monkeypatch.setattr(ls_mod, "_workspace_root", lambda _server: tmp_path)
        # Project-aware refresh requires a poliglot.yml at the project root;
        # the test's stub validate_project doesn't read it, but the gate does.
        (tmp_path / "poliglot.yml").touch()

        # Stub validate_project so we control diagnostic shape.
        from plgt.services.diagnostics import DiagnosticBag
        from plgt.services.validation_pipeline import ValidationResult

        first_bag = DiagnosticBag()
        first_bag.error("PLGT_E0001", "boom", path="spec/foo.ttl", line=1, col=1)
        first_bag.error("PLGT_E0001", "bang", path="spec/bar.ttl", line=2, col=2)
        monkeypatch.setattr(
            ls_mod,
            "validate_project",
            lambda _root, **_kwargs: ValidationResult(
                diagnostics=first_bag, assembled=None
            ),
        )

        published_calls: list[tuple[str, int]] = []
        original = server.text_document_publish_diagnostics

        def capture(params):
            published_calls.append((params.uri, len(params.diagnostics)))
            return original(params)

        monkeypatch.setattr(
            server, "text_document_publish_diagnostics", capture, raising=False
        )

        # First run: two files have errors.
        ls_mod._refresh_validation(server)
        first_uris = {uri for uri, _ in published_calls}
        assert any("foo.ttl" in uri for uri in first_uris)
        assert any("bar.ttl" in uri for uri in first_uris)

        # Second run: only foo.ttl still has errors. bar.ttl should be cleared
        # by an empty-list publish.
        second_bag = DiagnosticBag()
        second_bag.error("PLGT_E0001", "boom", path="spec/foo.ttl", line=1, col=1)
        monkeypatch.setattr(
            ls_mod,
            "validate_project",
            lambda _root, **_kwargs: ValidationResult(
                diagnostics=second_bag, assembled=None
            ),
        )

        published_calls.clear()
        ls_mod._refresh_validation(server)

        cleared = [(uri, n) for uri, n in published_calls if n == 0]
        assert any("bar.ttl" in uri for uri, _ in cleared), (
            "bar.ttl should have been cleared with an empty diagnostics list"
        )
        kept = [(uri, n) for uri, n in published_calls if n > 0]
        assert any("foo.ttl" in uri for uri, _ in kept)


class TestMultiMatrixDefinitionLookup:
    """``_locate_definition_site`` must walk every matrix's spec directory
    in the project, then fall through to the deps cache. The original
    impl only walked ``workspace_root / "spec"`` — multi-matrix projects
    and cached system-matrix terms went unresolved.
    """

    def test_finds_definition_in_nested_matrix_spec(self, tmp_path) -> None:
        from plgt.services import lsp_server as ls_mod

        # Multi-matrix layout: each matrix lives in its own subdir with its own spec/.
        core = tmp_path / "core" / "spec"
        core.mkdir(parents=True)
        (core / "ontology.ttl").write_text(
            "@prefix ex: <https://example.com/spec#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            'ex:Issue rdfs:label "Issue" .\n'
        )

        server = ls_mod.create_server()
        from unittest import mock

        with mock.patch.object(ls_mod, "_workspace_root", return_value=tmp_path):
            from rdflib import Graph

            location = ls_mod._locate_definition_site(
                server, "https://example.com/spec#Issue", Graph()
            )

        assert location is not None
        assert "ontology.ttl" in location.uri

    def test_falls_through_to_deps_cache_when_workspace_has_no_match(
        self, tmp_path
    ) -> None:
        from plgt.services import lsp_server as ls_mod

        deps_dir = (
            tmp_path / ".matrix" / "deps" / "_registry" / "poliglot" / "os" / "1.0.0"
        )
        deps_dir.mkdir(parents=True)
        (deps_dir / "ontology.ttl").write_text(
            "@prefix plgt: <https://poliglot.io/os/spec#> .\n"
            "@prefix plgt-proc: <https://poliglot.io/os/spec/processes#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            'plgt:Action rdfs:label "Action" .\n'
        )

        server = ls_mod.create_server()
        from unittest import mock

        with mock.patch.object(ls_mod, "_workspace_root", return_value=tmp_path):
            from rdflib import Graph

            location = ls_mod._locate_definition_site(
                server, "https://poliglot.io/os/spec#Action", Graph()
            )

        assert location is not None
        assert "ontology.ttl" in location.uri


class TestPrefixTableForDocument:
    """``.rq`` files resolve prefixed names against the owning matrix's
    own ``matrix.ttl`` prefix table, not the merged global one. Plan
    §140-146 specifies shadowing rules.
    """

    def test_rq_file_uses_matrix_ttl_prefixes(self, tmp_path) -> None:
        from plgt.services import lsp_server as ls_mod
        from rdflib import Graph

        matrix_dir = tmp_path / "issues" / "spec"
        matrix_dir.mkdir(parents=True)
        (matrix_dir / "matrix.ttl").write_text(
            "@prefix matrix-local: <https://internal.example/issues#> .\n"
            "@prefix plgt-mtx: <https://poliglot.io/os/spec/matrix#> .\n"
            "<https://internal.example/issues#> a plgt-mtx:Matrix .\n"
        )
        scripts = matrix_dir / "scripts"
        scripts.mkdir()
        rq_file = scripts / "find.rq"
        rq_file.write_text("SELECT ?x WHERE { ?x matrix-local:something ?y }\n")

        server = ls_mod.create_server()
        # Global graph has a totally different binding; the matrix-local prefix should
        # win because the .rq file is under that matrix.
        graph = Graph()
        graph.bind("matrix-local", "https://wrong.example/")

        table = ls_mod._prefix_table_for_document(server, rq_file.as_uri(), graph)
        assert table.get("matrix-local") == "https://internal.example/issues#"

    def test_ttl_file_uses_global_graph_prefixes(self, tmp_path) -> None:
        from plgt.services import lsp_server as ls_mod
        from rdflib import Graph

        ttl_file = tmp_path / "ontology.ttl"
        ttl_file.write_text("")

        server = ls_mod.create_server()
        graph = Graph()
        graph.bind("ex", "https://example.com#")

        table = ls_mod._prefix_table_for_document(server, ttl_file.as_uri(), graph)
        assert table.get("ex") == "https://example.com#"


class TestLspWorkspaceMode:
    """LSP reads the default workspace from plgt config on initialize and
    threads it through to ``validate_project`` and the lockfile-missing
    popup. Editor restart is required to pick up a new default; a future
    LSP option can override per-session.
    """

    def test_workspace_resolution_is_cached(self, monkeypatch) -> None:
        from plgt.services import lsp_server as ls_mod

        call_count = {"n": 0}

        def fake_resolve(**_kw):
            call_count["n"] += 1
            return "dev"

        # Patch the import inside _resolve_workspace_for_server.
        import plgt.utils.workspace_mode as wm

        monkeypatch.setattr(wm, "resolve_workspace_mode", fake_resolve)

        server = ls_mod.create_server()
        first = ls_mod._resolve_workspace_for_server(server)
        second = ls_mod._resolve_workspace_for_server(server)
        assert first == "dev"
        assert second == "dev"
        # Cached: the underlying resolver fires exactly once per server lifetime.
        assert call_count["n"] == 1

    def test_refresh_validation_passes_workspace_to_validate_project(
        self, tmp_path, monkeypatch
    ) -> None:
        from plgt.services import lsp_server as ls_mod
        from plgt.services.diagnostics import DiagnosticBag
        from plgt.services.validation_pipeline import ValidationResult

        captured: dict = {}

        def fake_validate(project_dir, **kwargs):
            captured["project_dir"] = project_dir
            captured["workspace"] = kwargs.get("workspace")
            return ValidationResult(diagnostics=DiagnosticBag(), assembled=None)

        monkeypatch.setattr(ls_mod, "validate_project", fake_validate)
        monkeypatch.setattr(ls_mod, "_workspace_root", lambda _s: tmp_path)
        (tmp_path / "poliglot.yml").touch()
        import plgt.utils.workspace_mode as wm

        monkeypatch.setattr(wm, "resolve_workspace_mode", lambda **_: "dev")

        server = ls_mod.create_server()
        ls_mod._refresh_validation(server)
        assert captured["project_dir"] == tmp_path
        assert captured["workspace"] == "dev"

    def test_refresh_validation_falls_back_to_registry_when_no_default(
        self, tmp_path, monkeypatch
    ) -> None:
        from plgt.services import lsp_server as ls_mod
        from plgt.services.diagnostics import DiagnosticBag
        from plgt.services.validation_pipeline import ValidationResult

        captured: dict = {}

        def fake_validate(project_dir, **kwargs):
            captured["workspace"] = kwargs.get("workspace")
            return ValidationResult(diagnostics=DiagnosticBag(), assembled=None)

        monkeypatch.setattr(ls_mod, "validate_project", fake_validate)
        monkeypatch.setattr(ls_mod, "_workspace_root", lambda _s: tmp_path)
        (tmp_path / "poliglot.yml").touch()
        import plgt.utils.workspace_mode as wm

        monkeypatch.setattr(wm, "resolve_workspace_mode", lambda **_: None)

        server = ls_mod.create_server()
        ls_mod._refresh_validation(server)
        assert captured["workspace"] is None


class TestFormatHover:
    def test_includes_definition_and_types(self) -> None:
        description = TermDescription(
            uri="https://example.com/spec/issues#Issue",
            label="Issue",
            comment=None,
            definition="A Widget issue.",
            types=["http://www.w3.org/2002/07/owl#Class"],
            subclass_of=[],
            properties=[],
            subclasses=[],
            defined_in=None,
        )
        rendered = _format_hover(description)
        assert "**https://example.com/spec/issues#Issue**" in rendered
        assert "_Issue_" in rendered
        assert "A Widget issue." in rendered
        assert "**type:**" in rendered

    def test_falls_back_to_comment_when_no_definition(self) -> None:
        description = TermDescription(
            uri="https://example.com/spec/issues#X",
            label=None,
            comment="A short comment.",
            definition=None,
            types=[],
            subclass_of=[],
            properties=[],
            subclasses=[],
            defined_in=None,
        )
        rendered = _format_hover(description)
        assert "A short comment." in rendered


class TestMultiProjectWorkspace:
    """An editor workspace can contain multiple matrix projects (each with
    their own ``poliglot.yml``). The LSP must find the project root by
    walking up from the file being edited, not by assuming the workspace
    root is a project.
    """

    def test_find_project_root_walks_up_from_file(self, tmp_path) -> None:
        from plgt.services import lsp_server as ls_mod

        workspace = tmp_path
        project = workspace / "ops" / "widget"
        spec = project / "core" / "spec"
        spec.mkdir(parents=True)
        (project / "poliglot.yml").touch()
        ttl = spec / "actions.ttl"
        ttl.touch()

        assert ls_mod._find_project_root(ttl, workspace) == project.resolve()

    def test_find_project_root_returns_none_outside_workspace(self, tmp_path) -> None:
        from plgt.services import lsp_server as ls_mod

        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        f = outside / "x.ttl"
        f.touch()

        assert ls_mod._find_project_root(f, workspace) is None

    def test_find_project_root_returns_none_when_no_poliglot_yml(
        self, tmp_path
    ) -> None:
        from plgt.services import lsp_server as ls_mod

        spec = tmp_path / "ops" / "widget" / "spec"
        spec.mkdir(parents=True)
        f = spec / "actions.ttl"
        f.touch()
        # No poliglot.yml anywhere on the way up.
        assert ls_mod._find_project_root(f, tmp_path) is None

    def test_refresh_validation_for_uri_targets_subproject(
        self, tmp_path, monkeypatch
    ) -> None:
        """When the workspace root has no poliglot.yml but a sub-directory
        does, didOpen-style refresh must target the sub-directory.
        """
        from plgt.services import lsp_server as ls_mod
        from plgt.services.diagnostics import DiagnosticBag
        from plgt.services.validation_pipeline import ValidationResult

        workspace = tmp_path
        project = workspace / "ops" / "widget"
        spec = project / "spec"
        spec.mkdir(parents=True)
        (project / "poliglot.yml").touch()
        ttl = spec / "actions.ttl"
        ttl.touch()

        captured: dict = {}

        def fake_validate(project_dir, **kwargs):
            captured["project_dir"] = project_dir
            return ValidationResult(diagnostics=DiagnosticBag(), assembled=None)

        monkeypatch.setattr(ls_mod, "validate_project", fake_validate)
        monkeypatch.setattr(ls_mod, "_workspace_root", lambda _s: workspace)
        import plgt.utils.workspace_mode as wm

        monkeypatch.setattr(wm, "resolve_workspace_mode", lambda **_: None)

        server = ls_mod.create_server()
        ls_mod._refresh_validation_for_uri(server, ttl.as_uri())

        assert captured["project_dir"] == project.resolve()
