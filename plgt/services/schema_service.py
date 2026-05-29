"""Schema query operations against the assembled validation graph.

Used by ``plgt schema {describe|list|search}`` and by the LSP for hover
and completion. The functions consume the same ``Graph`` produced by
``validation_pipeline.validate_project`` so the schema commands and
validation see the same dep set and prefix table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import RDF, RDFS, URIRef
from rdflib.namespace import SKOS

if TYPE_CHECKING:
    from rdflib import Graph


DCT_DESCRIPTION = URIRef("http://purl.org/dc/terms/description")


@dataclass(frozen=True)
class TermDescription:
    """Result of `schema describe <uri>`. Fields are typed for the JSON
    output; missing values come out as None / [] consistently.
    """

    uri: str
    label: str | None
    comment: str | None
    definition: str | None
    types: list[str]
    subclass_of: list[str]
    properties: list[str]  # property URIs whose rdfs:domain includes this term
    subclasses: list[str]
    defined_in: str | None

    def to_dict(self) -> dict:
        return {
            "uri": self.uri,
            "label": self.label,
            "comment": self.comment,
            "definition": self.definition,
            "types": self.types,
            "subclassOf": self.subclass_of,
            "properties": self.properties,
            "subclasses": self.subclasses,
            "definedIn": self.defined_in,
        }


def resolve_term(graph: Graph, term: str) -> str:
    """Expand a prefixed name (``plgt-act:Action``) to a full URI using the
    graph's namespace bindings. Returns the input unchanged if it already
    looks like a full URI or no matching prefix is registered.
    """
    if "://" in term or term.startswith("<"):
        return term.strip("<>")
    if ":" not in term:
        return term
    prefix, _, local = term.partition(":")
    for bound_prefix, namespace in graph.namespaces():
        if bound_prefix == prefix:
            return f"{namespace}{local}"
    return term


def describe_term(graph: Graph, term_uri: str) -> TermDescription | None:
    """Return a structured description of ``term_uri``, or None if no
    triples mention it. Accepts either a full URI or a prefixed name
    (``plgt-act:Action``) — prefixes are resolved via the assembled graph's
    namespace bindings.

    For classes, lists subclasses and properties whose domain includes the
    class. For all terms, returns rdfs:label, rdfs:comment,
    skos:definition, rdf:type values, and rdfs:isDefinedBy.
    """
    resolved = resolve_term(graph, term_uri)
    term = URIRef(resolved)

    label = _first_literal(graph, term, RDFS.label)
    comment = _first_literal(graph, term, RDFS.comment)
    definition = _first_literal(graph, term, SKOS.definition) or _first_literal(
        graph, term, DCT_DESCRIPTION
    )
    types = sorted({str(t) for t in graph.objects(term, RDF.type)})
    subclass_of = sorted({str(t) for t in graph.objects(term, RDFS.subClassOf)})
    properties = sorted({str(prop) for prop in graph.subjects(RDFS.domain, term)})
    subclasses = sorted({str(sub) for sub in graph.subjects(RDFS.subClassOf, term)})
    defined_in_uri = next(graph.objects(term, RDFS.isDefinedBy), None)
    defined_in = str(defined_in_uri) if defined_in_uri else None

    if not any(
        (label, comment, definition, types, subclass_of, properties, subclasses)
    ):
        return None

    return TermDescription(
        uri=resolved,
        label=label,
        comment=comment,
        definition=definition,
        types=types,
        subclass_of=subclass_of,
        properties=properties,
        subclasses=subclasses,
        defined_in=defined_in,
    )


def list_terms(
    graph: Graph,
    *,
    type_filter: str | None = None,
) -> list[dict]:
    """Enumerate every term that's either a class, property, or named
    individual in the graph. Optionally filter by ``rdf:type`` URI
    (e.g. ``plgt-act:Action``).

    Returns a list of ``{uri, label, type}`` dicts sorted by URI.
    """
    rows: list[tuple[str, str | None, list[str]]] = []
    for subject in _enumerated_terms(graph, type_filter):
        if not isinstance(subject, URIRef):
            continue
        label = _first_literal(graph, subject, RDFS.label)
        types = sorted({str(t) for t in graph.objects(subject, RDF.type)})
        rows.append((str(subject), label, types))
    rows.sort()
    return [{"uri": uri, "label": label, "types": types} for uri, label, types in rows]


def search_terms(graph: Graph, query: str) -> list[dict]:
    """Hybrid label-fuzzy + comment-substring search.

    Per-term scoring:

    * Label match: ``difflib.SequenceMatcher`` ratio between query and
      label (case-insensitive). Adds the ratio as a score, scaled so a
      perfect label match outranks any comment-only hit.
    * Comment / definition match: substring presence adds a smaller
      score. Catches "the long description mentions this word"
      without ranking comment-only hits above name matches.

    Terms with no positive score are filtered out. Results sort by score
    descending, then by URI for stability. Returns ``[{uri, label,
    snippet, score}]``; the score is included so the LSP can present
    relevance ordering and tooling can do further filtering.
    """
    import difflib

    needle = query.lower().strip()
    if not needle:
        return []

    # Gather per-term text in one pass to avoid N graph walks.
    by_subject: dict[URIRef, dict[str, str]] = {}
    for subject, predicate, value in graph:
        if not isinstance(subject, URIRef):
            continue
        if predicate == RDFS.label:
            by_subject.setdefault(subject, {})["label"] = str(value)
        elif predicate == RDFS.comment:
            by_subject.setdefault(subject, {})["comment"] = str(value)
        elif predicate in (SKOS.definition, DCT_DESCRIPTION):
            by_subject.setdefault(subject, {})["definition"] = str(value)

    # Short queries (1-2 chars) get a substring-only mode: the difflib ratio is
    # mathematically near-zero against any reasonably long label, so the default threshold
    # would require BOTH label and comment substring matches to clear. That defeats prefix
    # search — typing `"l"` to find every term whose name starts with L should work. For
    # short queries, drop the ratio component and rely on substring presence in label OR
    # local-name (a strong, deterministic signal at this length).
    short_query = len(needle) <= 2

    scored: list[tuple[float, str, str | None, str]] = []
    for subject, fields in by_subject.items():
        label = fields.get("label")
        comment = fields.get("comment") or fields.get("definition") or ""
        local_name = str(subject).rsplit("#", 1)[-1].rsplit("/", 1)[-1]

        score = 0.0
        if short_query:
            # Prefix / substring match in label or local-name. Score is binary-ish so all
            # short-query hits sort together, then within the group ties break on URI.
            if (label and needle in label.lower()) or needle in local_name.lower():
                score += 1.5
            if comment and needle in comment.lower():
                score += 0.5
        else:
            # Label fuzzy (ratio scaled into [0, 1]). Use label OR local-name as the
            # comparison target: a term without an explicit label is still searchable by
            # its identifier.
            label_target = (label or local_name).lower()
            label_ratio = difflib.SequenceMatcher(None, needle, label_target).ratio()
            # Boost label-ratio score so it outranks pure comment hits at any reasonable
            # cutoff.
            score += label_ratio * 2.0
            # Substring presence in comment/definition (binary signal).
            if comment and needle in comment.lower():
                score += 0.5
            # Substring presence in label catches the npm-style "type the start" search;
            # the ratio alone underweights this because long labels with the needle as a
            # prefix still score modestly.
            if label and needle in label.lower():
                score += 0.5

        if score < 1.0:
            # Below this floor the match is too weak to be useful (random label-ratio
            # noise for unrelated identifiers tops out around 0.4 * 2.0 = 0.8).
            continue

        snippet_source = comment or label or local_name
        snippet = (
            snippet_source
            if len(snippet_source) < 200
            else snippet_source[:197] + "..."
        )
        scored.append((score, str(subject), label, snippet))

    scored.sort(key=lambda row: (-row[0], row[1]))
    return [
        {"uri": uri, "label": label, "snippet": snippet, "score": round(score, 3)}
        for score, uri, label, snippet in scored
    ]


# ---------------------------------------------------------------------------


def _first_literal(graph: Graph, subject: URIRef, predicate: URIRef) -> str | None:
    for value in graph.objects(subject, predicate):
        return str(value)
    return None


def _enumerated_terms(graph: Graph, type_filter: str | None) -> set:
    """Subjects we consider "terms" for listing: anything with an rdf:type.
    Filter narrows to subjects whose type equals ``type_filter``.
    """
    if type_filter:
        filter_uri = URIRef(type_filter)
        return set(graph.subjects(RDF.type, filter_uri))
    return set(graph.subjects(predicate=RDF.type))


__all__ = [
    "TermDescription",
    "describe_term",
    "list_terms",
    "search_terms",
]
