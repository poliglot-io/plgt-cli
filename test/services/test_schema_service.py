"""Unit tests for ``plgt.services.schema_service``.

Focuses on the Levenshtein-ranked ``search_terms`` behavior added in
Phase D: label matches outrank comment-only matches, exact-prefix typing
finds the term, irrelevant terms are filtered out.
"""

from __future__ import annotations

from plgt.services.schema_service import search_terms
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS, SKOS


def _graph_with_terms() -> Graph:
    g = Graph()
    g.add(
        (
            URIRef("https://example.com/spec#ListIssues"),
            RDFS.label,
            Literal("List Issues"),
        )
    )
    g.add(
        (
            URIRef("https://example.com/spec#ListIssues"),
            SKOS.definition,
            Literal("Return every issue in the workspace."),
        )
    )
    g.add(
        (
            URIRef("https://example.com/spec#CreateIssue"),
            RDFS.label,
            Literal("Create Issue"),
        )
    )
    g.add(
        (
            URIRef("https://example.com/spec#Random"),
            RDFS.comment,
            Literal("This action lists nothing of relevance to issues."),
        )
    )
    return g


class TestSearchTerms:
    def test_label_match_outranks_comment_match(self) -> None:
        results = search_terms(_graph_with_terms(), "issues")
        assert len(results) >= 2
        # ListIssues / CreateIssue should come before Random — label substring beats a
        # comment-only mention.
        top_uris = [r["uri"] for r in results[:2]]
        assert "https://example.com/spec#Random" not in top_uris

    def test_exact_match_returns_score_above_threshold(self) -> None:
        results = search_terms(_graph_with_terms(), "list issues")
        assert results
        top = results[0]
        assert top["uri"] == "https://example.com/spec#ListIssues"
        assert top["score"] >= 1.5  # label-ratio (high) * 2 + label-substring 0.5

    def test_empty_query_returns_empty(self) -> None:
        assert search_terms(_graph_with_terms(), "") == []
        assert search_terms(_graph_with_terms(), "   ") == []

    def test_unrelated_query_returns_empty(self) -> None:
        # "zzz" has no label/comment/definition match and a low fuzzy ratio to every term.
        assert search_terms(_graph_with_terms(), "zzz") == []

    def test_results_include_score_field(self) -> None:
        results = search_terms(_graph_with_terms(), "issue")
        for row in results:
            assert "score" in row
            assert isinstance(row["score"], float)

    def test_short_query_does_substring_match_on_label(self) -> None:
        """1-2 char queries fall into substring-only mode so prefix typing
        ("l" to find every term starting with L) works instead of being
        dropped by the ratio threshold.
        """
        results = search_terms(_graph_with_terms(), "l")
        uris = [r["uri"] for r in results]
        # Both "List Issues" and "ListIssues" (local name) match the prefix "l".
        assert "https://example.com/spec#ListIssues" in uris

    def test_short_query_against_local_name_only_term(self) -> None:
        """A term with no rdfs:label still matches by local-name substring
        in short-query mode.
        """
        from rdflib import Graph, Literal, URIRef
        from rdflib.namespace import RDFS

        g = Graph()
        # No label — only a comment. Local name "Anchor" should still match `"a"`.
        g.add(
            (
                URIRef("https://example.com/spec#Anchor"),
                RDFS.comment,
                Literal("Something."),
            )
        )
        results = search_terms(g, "a")
        uris = [r["uri"] for r in results]
        assert "https://example.com/spec#Anchor" in uris
