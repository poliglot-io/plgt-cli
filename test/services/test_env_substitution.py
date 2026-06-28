"""Unit tests for ``plgt.services.env_substitution``."""

from __future__ import annotations

import pytest
from plgt.services.env_substitution import substitute_env_text, substitute_env_vars
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS, XSD, Namespace

EX = Namespace("https://example.com/spec#")


# --------------------------------------------------------------------------- #
# substitute_env_text — the string-level grammar / precedence
# --------------------------------------------------------------------------- #


def test_env_value_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com")
    assert (
        substitute_env_text("${API_BASE_URL:http://localhost:8080}/v1")
        == "https://api.example.com/v1"
    )


def test_default_used_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_BASE_URL", raising=False)
    assert (
        substitute_env_text("${API_BASE_URL:http://localhost:8080}/v1")
        == "http://localhost:8080/v1"
    )


def test_empty_string_when_no_env_and_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("API_BASE_URL", raising=False)
    # The placeholder collapses to "".
    assert substitute_env_text("${API_BASE_URL}/v1") == "/v1"


def test_substitution_is_anywhere_not_full_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The placeholder is an embedded substring with a suffix — a full-literal-match
    # rule would leave this untouched, which is the case this guards against.
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com")
    assert (
        substitute_env_text("${API_BASE_URL}/v1/items")
        == "https://api.example.com/v1/items"
    )


def test_multiple_placeholders_in_one_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "h.example")
    monkeypatch.setenv("PORT", "8443")
    assert (
        substitute_env_text("https://${HOST}:${PORT}/x") == "https://h.example:8443/x"
    )


def test_no_placeholder_unchanged() -> None:
    assert substitute_env_text("https://example.com/v1/items") == (
        "https://example.com/v1/items"
    )


# --------------------------------------------------------------------------- #
# substitute_env_vars — graph-level rewrite (literals only, type-preserving)
# --------------------------------------------------------------------------- #


def test_rewrites_typed_literal_and_preserves_datatype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_BASE_URL", "https://api.example.com")
    g = Graph()
    subj = EX.Endpoint
    g.add(
        (
            subj,
            EX.endpointUrl,
            Literal("${API_BASE_URL:http://localhost:8080}/v1", datatype=XSD.anyURI),
        )
    )

    substitute_env_vars(g)

    objs = list(g.objects(subj, EX.endpointUrl))
    assert len(objs) == 1
    (result,) = objs
    assert isinstance(result, Literal)
    assert str(result) == "https://api.example.com/v1"
    assert result.datatype == XSD.anyURI, "datatype must be preserved"


def test_preserves_language_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NAME", "Example")
    g = Graph()
    subj = EX.Thing
    g.add((subj, RDFS.label, Literal("${NAME} service", lang="en")))

    substitute_env_vars(g)

    (result,) = list(g.objects(subj, RDFS.label))
    assert str(result) == "Example service"
    assert result.language == "en", "language tag must be preserved"


def test_leaves_iri_objects_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # A URIRef object is never a substitution target even if it somehow contained the
    # token — only literal objects are processed.
    monkeypatch.setenv("X", "should-not-appear")
    g = Graph()
    subj = EX.Thing
    iri = URIRef("https://example.com/spec#Ref")
    g.add((subj, EX.ref, iri))

    substitute_env_vars(g)

    assert (subj, EX.ref, iri) in g


def test_idempotent_when_no_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    g = Graph()
    subj = EX.Thing
    lit = Literal("https://example.com/v1/items", datatype=XSD.anyURI)
    g.add((subj, EX.endpointUrl, lit))

    before = set(g)
    substitute_env_vars(g)
    assert set(g) == before, "literals without ${...} must be left exactly as-is"


def test_returns_graph_for_chaining(monkeypatch: pytest.MonkeyPatch) -> None:
    g = Graph()
    assert substitute_env_vars(g) is g
