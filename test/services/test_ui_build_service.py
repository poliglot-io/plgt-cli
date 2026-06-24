"""Tests for the UI build service's generated RDF.

These tests render the real ``components.ttl.j2`` template (no mocks) and
parse the output with rdflib to assert that the generated bundle artifact
carries its media type under the spec-mandated predicate.
"""

from rdflib import Graph, Namespace, URIRef

from plgt.services.ui_build_service import generate_component_ttl

PLGT_UI = Namespace("https://poliglot.io/os/spec/ui#")
PLGT_BUILD = Namespace("https://poliglot.io/os/spec/build#")
PLGT_CNT = Namespace("https://poliglot.io/os/spec/content#")

# Note: ``Namespace.format`` is a built-in method, so the ``format`` term must
# be referenced via item access (``NS["format"]``), not attribute access.
CNT_FORMAT = PLGT_CNT["format"]
BUILD_FORMAT = PLGT_BUILD["format"]

MATRIX_URI = "https://example.org/matrix/teams#"


def _bundle_graph(exports: list[str]) -> tuple[Graph, URIRef]:
    ttl = generate_component_ttl(exports, MATRIX_URI)
    graph = Graph()
    graph.parse(data=ttl, format="turtle")
    bundle = URIRef(MATRIX_URI + "components")
    return graph, bundle


def test_bundle_media_type_uses_content_format_predicate() -> None:
    """The bundle's media type must be emitted under plgt-cnt:format.

    The spec's BuildArtifactShape requires ``plgt-cnt:format`` with an
    ``sh:class plgt-cnt:MediaType``; ``plgt-build:format`` is not defined.
    """
    graph, bundle = _bundle_graph(["Foo"])

    media_type = URIRef(PLGT_CNT.ApplicationJavascript)
    assert (bundle, CNT_FORMAT, media_type) in graph


def test_bundle_does_not_use_build_format_predicate() -> None:
    """The wrong predicate (plgt-build:format) must not be emitted."""
    graph, bundle = _bundle_graph(["Foo"])

    assert (bundle, BUILD_FORMAT, None) not in graph


def test_bundle_path_unchanged() -> None:
    """The artifact path remains under the correct plgt-build:path predicate."""
    graph, bundle = _bundle_graph(["Foo"])

    paths = list(graph.objects(bundle, PLGT_BUILD.path))
    assert [str(p) for p in paths] == ["components.js"]


def test_bundle_is_typed() -> None:
    """The bundle is typed as a plgt-ui:Bundle."""
    graph, bundle = _bundle_graph([])

    assert (
        bundle,
        URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
        PLGT_UI.Bundle,
    ) in graph
