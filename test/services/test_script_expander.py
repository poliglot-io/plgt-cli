"""Unit tests for ``plgt.services.script_expander``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from plgt.core.exceptions import ValidationError
from plgt.services.script_expander import (
    SCRIPT_SCHEME,
    SPARQL_BEARING_PREDICATES,
    expand_script_refs,
    script_predicates_in_graph,
)
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import Namespace

if TYPE_CHECKING:
    from pathlib import Path


LIN_ISS = Namespace("https://example.com/spec/issues#")
PLGT = Namespace("https://poliglot.io/os/spec#")
PROC = Namespace("https://poliglot.io/os/spec/processes#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")


def _make_graph_with_prefixes() -> Graph:
    g = Graph()
    g.bind("wgt-iss", LIN_ISS)
    g.bind("plgt", PLGT)
    g.bind("plgt-proc", PROC)
    g.bind("rdfs", RDFS)
    return g


def _write_script(matrix_dir: Path, relative: str, content: str) -> None:
    target = matrix_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


class TestExpandBasics:
    def test_replaces_script_uri_with_literal_holding_inlined_sparql(
        self, tmp_path: Path
    ) -> None:
        _write_script(
            tmp_path,
            "scripts/list-issues-commit.rq",
            "INSERT { ?s ?p ?o }\nWHERE { GRAPH ?cs { ?s ?p ?o } FILTER NOT EXISTS { ?s ?p ?o } }\n",
        )

        g = _make_graph_with_prefixes()
        commit = LIN_ISS["ListIssuesCommit"]
        g.add(
            (
                commit,
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}scripts/list-issues-commit.rq"),
            )
        )

        expand_script_refs(g, tmp_path)

        objects = list(g.objects(commit, PROC.update))
        assert len(objects) == 1
        assert isinstance(objects[0], Literal)
        # The body shouldn't reference wgt-iss: anywhere, so no PREFIX line is emitted.
        assert "PREFIX" not in str(objects[0])
        assert "INSERT" in str(objects[0])

    def test_injects_only_referenced_prefixes(self, tmp_path: Path) -> None:
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            'INSERT { wgt-iss:status "ok" }\nWHERE {}\n',
        )

        g = _make_graph_with_prefixes()
        # Add an extra prefix the script doesn't use; it MUST NOT appear in
        # the inlined PREFIX block.
        other = Namespace("https://example.com/other#")
        g.bind("other", other)
        commit = LIN_ISS["X"]
        g.add((commit, PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))

        expand_script_refs(g, tmp_path)

        body = str(next(g.objects(commit, PROC.update)))
        assert "PREFIX wgt-iss: <https://example.com/spec/issues#>" in body
        assert "other:" not in body

    def test_script_local_prefix_shadows_matrix_prefix(self, tmp_path: Path) -> None:
        # Script declares its own wgt-iss: prefix pointing somewhere else.
        # The matrix-table version must NOT be added in addition; the
        # script-local one wins.
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            "PREFIX wgt-iss: <https://example.com/shadowed#>\n"
            'INSERT { wgt-iss:status "ok" }\nWHERE {}\n',
        )

        g = _make_graph_with_prefixes()
        commit = LIN_ISS["X"]
        g.add((commit, PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))

        expand_script_refs(g, tmp_path)

        body = str(next(g.objects(commit, PROC.update)))
        # No matrix-table PREFIX was prepended — the script's own one already
        # covers wgt-iss:.
        assert body.count("PREFIX wgt-iss:") == 1
        assert "https://example.com/shadowed#" in body


class TestPrefixScanningIgnoresStringsAndComments:
    """Regressions for the JSON DSL case: prefixed-name-shaped substrings
    inside string literals or `#` comments must NOT trigger PREFIX injection
    in the inlined output.
    """

    def test_double_quoted_string_does_not_trigger_prefix(self, tmp_path: Path) -> None:
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            'INSERT { ?s ?p "wgt-iss:not-a-qname" } WHERE { ?s ?p ?o }\n',
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))

        expand_script_refs(g, tmp_path)

        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        assert "PREFIX wgt-iss:" not in body

    def test_triple_quoted_string_does_not_trigger_prefix(self, tmp_path: Path) -> None:
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            'INSERT { ?s ?p """wgt-iss:stillNotAQname""" } WHERE {}\n',
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))
        expand_script_refs(g, tmp_path)
        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        assert "PREFIX wgt-iss:" not in body

    def test_comment_does_not_trigger_prefix(self, tmp_path: Path) -> None:
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            "# Note: wgt-iss:wat — not a real qname use\nSELECT * WHERE { ?s ?p ?o }\n",
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))
        expand_script_refs(g, tmp_path)
        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        assert "PREFIX wgt-iss:" not in body

    def test_real_use_still_triggers_prefix(self, tmp_path: Path) -> None:
        """Sanity check: stripping strings/comments hasn't broken the
        normal case where the prefixed name IS in structural SPARQL.
        """
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            "INSERT { ?s rdfs:label wgt-iss:Issue } WHERE {}\n",
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))
        expand_script_refs(g, tmp_path)
        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        assert "PREFIX wgt-iss: <https://example.com/spec/issues#>" in body
        assert "PREFIX rdfs:" in body

    def test_uri_fragment_is_not_treated_as_comment(self, tmp_path: Path) -> None:
        """A `#fragment` inside `<https://example.com#fragment>` must not be
        misread as the start of a SPARQL line comment. Otherwise the URI is
        broken, downstream regex matchers fail, and a script-local PREFIX
        declaration (which contains exactly this pattern) would be missed.
        """
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            "PREFIX wgt-iss: <https://example.com/shadowed#>\n"
            "SELECT * WHERE { ?s a wgt-iss:Issue }\n",
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))
        expand_script_refs(g, tmp_path)
        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        # The script-local PREFIX must be detected, so the matrix's
        # https://example.com/spec/issues# binding is NOT added on top.
        assert body.count("PREFIX wgt-iss:") == 1
        assert "https://example.com/shadowed#" in body
        assert "https://example.com/spec/issues#" not in body

    def test_mixed_real_and_string_use(self, tmp_path: Path) -> None:
        """Prefixed name appears in BOTH a string literal AND a real SPARQL
        position. The real use should still drive injection; the string-
        literal use is incidental.
        """
        _write_script(
            tmp_path,
            "scripts/foo.rq",
            'INSERT { ?s rdfs:label "wgt-iss:in-a-string" }\n'
            "WHERE { ?s a wgt-iss:Issue }\n",
        )
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["X"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq")))
        expand_script_refs(g, tmp_path)
        body = str(next(g.objects(LIN_ISS["X"], PROC.update)))
        # wgt-iss appears in real use, so the PREFIX is correctly emitted.
        assert "PREFIX wgt-iss:" in body
        # Exactly once — the string-literal use must NOT cause a duplicate.
        assert body.count("PREFIX wgt-iss:") == 1


class TestExpandValidation:
    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        _write_script(tmp_path / "..", "evil.rq", "INSERT { ?s ?p ?o } WHERE {}")

        g = _make_graph_with_prefixes()
        g.add(
            (
                LIN_ISS["X"],
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}../evil.rq"),
            )
        )
        with pytest.raises(ValidationError, match="escapes the matrix directory"):
            expand_script_refs(g, tmp_path)

    def test_rejects_authority_component(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        g.add(
            (
                LIN_ISS["X"],
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}//shared/foo.rq"),
            )
        )
        with pytest.raises(ValidationError, match="authority component"):
            expand_script_refs(g, tmp_path)

    def test_rejects_non_rq_extension(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        g.add(
            (
                LIN_ISS["X"],
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}scripts/foo.sparql"),
            )
        )
        with pytest.raises(ValidationError, match=r"must reference a \.rq file"):
            expand_script_refs(g, tmp_path)

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        """`script:///etc/passwd` (three slashes) flattens to /etc/passwd
        as the path component. Resolution must reject this as a path-
        traversal attempt, not silently serve filesystem secrets.
        """
        g = _make_graph_with_prefixes()
        g.add(
            (
                LIN_ISS["X"],
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}/etc/passwd.rq"),
            )
        )
        with pytest.raises(ValidationError, match="escapes the matrix directory"):
            expand_script_refs(g, tmp_path)

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        g.add(
            (
                LIN_ISS["X"],
                PROC.update,
                URIRef(f"{SCRIPT_SCHEME}scripts/nonexistent.rq"),
            )
        )
        with pytest.raises(ValidationError, match="references missing file"):
            expand_script_refs(g, tmp_path)


class TestExpandPreservesUnaffected:
    def test_literal_objects_are_not_touched(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        original = Literal("INSERT { ?s ?p ?o } WHERE { ?s ?p ?o }")
        g.add((LIN_ISS["X"], PROC.update, original))

        expand_script_refs(g, tmp_path)

        objects = list(g.objects(LIN_ISS["X"], PROC.update))
        assert objects == [original]

    def test_unrelated_predicates_pass_through(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        # An rdfs:label that happens to be a URIRef to a script:// path should
        # NOT be expanded — only known SPARQL-bearing predicates are
        # recognised.
        g.add(
            (
                LIN_ISS["X"],
                RDFS.label,
                URIRef(f"{SCRIPT_SCHEME}scripts/foo.rq"),
            )
        )
        # Write the file so a buggy implementation that DID try to load it
        # would succeed silently.
        _write_script(tmp_path, "scripts/foo.rq", "noop")

        expand_script_refs(g, tmp_path)

        obj = next(g.objects(LIN_ISS["X"], RDFS.label))
        assert isinstance(obj, URIRef)
        assert str(obj) == f"{SCRIPT_SCHEME}scripts/foo.rq"


class TestMultipleRefs:
    def test_multiple_script_refs_in_one_graph(self, tmp_path: Path) -> None:
        """The two-pass collect-then-mutate design must handle graphs with
        multiple script:// refs without corrupting iteration.
        """
        _write_script(tmp_path, "scripts/a.rq", "INSERT { ?s ?p ?o } WHERE {}\n")
        _write_script(
            tmp_path, "scripts/b.rq", "SELECT * WHERE { ?s a wgt-iss:Issue }\n"
        )

        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["A"], PROC.update, URIRef(f"{SCRIPT_SCHEME}scripts/a.rq")))
        g.add((LIN_ISS["B"], PLGT.fromJSON, URIRef(f"{SCRIPT_SCHEME}scripts/b.rq")))

        expand_script_refs(g, tmp_path)

        a = str(next(g.objects(LIN_ISS["A"], PROC.update)))
        b = str(next(g.objects(LIN_ISS["B"], PLGT.fromJSON)))
        assert "INSERT" in a
        assert "SELECT" in b
        assert "PREFIX wgt-iss:" in b
        assert "PREFIX wgt-iss:" not in a


class TestPredicateRegistry:
    def test_known_predicates_present(self) -> None:
        # Sanity check: every predicate registered in SPARQL_BEARING_PREDICATES
        # uses the plgt: or plgt-iam: namespace and a known fragment.
        for pred in SPARQL_BEARING_PREDICATES:
            # plgt: root vocab is at /os/spec#, sub-vocabs at /os/spec/<area>#.
            assert str(pred).startswith("https://poliglot.io/os/spec")

    def test_script_predicates_in_graph_enumeration(self, tmp_path: Path) -> None:
        g = _make_graph_with_prefixes()
        g.add((LIN_ISS["A"], PROC.update, Literal("...")))
        g.add((LIN_ISS["B"], PLGT.fromJSON, Literal("...")))
        g.add((LIN_ISS["C"], RDFS.label, Literal("not a sparql body")))

        predicates = set(script_predicates_in_graph(g))
        assert PROC.update in predicates
        assert PLGT.fromJSON in predicates
        assert RDFS.label not in predicates
