"""Regression tests for RDF-star token handling in the SPARQL formatter.

SPARQL 1.2 / RDF-star introduces four delimiter tokens that the formatter
must treat as atomic, never splitting them with whitespace:

* ``<<``  — reifier / quoted-triple open
* ``>>``  — reifier / quoted-triple close
* ``<<(`` — triple-term open
* ``)>>`` — triple-term close

Earlier the formatter's lexer had no concept of these tokens. The ``<``
IRI reader greedily swallowed ``<< s p o >`` as a single ``<...>`` IRI, and
the operator pass split the trailing ``>>`` into two ``>`` tokens — so
``<< :s :p :o >>`` was rewritten to the invalid ``<< :s :p :o > >``. These
tests pin the correct, atomic, idempotent behaviour using only neutral
``ex:`` / default-prefix IRIs.
"""

from plgt.services.formatter.lexer import TT, tokenize
from plgt.services.formatter.sparql import format_sparql

# Tokens that, if they ever appear in formatted output, mean a delimiter was
# split by stray whitespace.
_SPLIT_ARTEFACTS = ("> >", "< <", "< <(", ") > >", ")> >", ")>  >")


def _assert_no_split_artefacts(text: str) -> None:
    for bad in _SPLIT_ARTEFACTS:
        assert bad not in text, f"delimiter split artefact {bad!r} found in:\n{text}"


def test_reifier_tokens_are_atomic_in_lexer() -> None:
    """``<<`` and ``>>`` lex as single QT_OPEN / QT_CLOSE tokens, with the
    inner ``s p o`` terms tokenised normally between them."""
    tokens = tokenize("<< :s :p :o >> :conf 0.9", sparql_mode=True)
    types = [t.type for t in tokens]
    assert types == [
        TT.QT_OPEN,
        TT.PNAME,
        TT.PNAME,
        TT.PNAME,
        TT.QT_CLOSE,
        TT.PNAME,
        TT.NUMBER,
    ]
    assert tokens[0].text == "<<"
    assert tokens[4].text == ">>"


def test_triple_term_tokens_are_atomic_in_lexer() -> None:
    """``<<(`` and ``)>>`` lex as single TT_OPEN / TT_CLOSE tokens. The
    longer ``<<(`` wins over ``<<``, and ``)>>`` wins over a bare ``)``."""
    tokens = tokenize("<<( ?s ?p ?o )>>", sparql_mode=True)
    types = [t.type for t in tokens]
    assert types == [
        TT.TT_OPEN,
        TT.VARIABLE,
        TT.VARIABLE,
        TT.VARIABLE,
        TT.TT_CLOSE,
    ]
    assert tokens[0].text == "<<("
    assert tokens[-1].text == ")>>"
    # The interior ``(`` / ``)`` are part of the atomic delimiters — no
    # spurious LPAREN/RPAREN tokens leak out.
    assert TT.LPAREN not in types
    assert TT.RPAREN not in types


def test_reifier_object_formats_without_splitting() -> None:
    """A reifier object ``<< :s :p :o >>`` round-trips with correct spacing
    and is never rewritten to ``<< :s :p :o > >``."""
    src = "ASK { << :s :p :o >> :conf 0.9 . }\n"
    result = format_sparql(src)
    _assert_no_split_artefacts(result)
    assert "<< :s :p :o >>" in result
    assert result == "ASK {\n    << :s :p :o >> :conf 0.9 .\n}\n"


def test_triple_term_formats_without_splitting() -> None:
    """A triple-term object ``<<( ?s ?p ?o )>>`` round-trips with correct
    spacing and is never rewritten to ``<<( ?s ?p ?o )> >``."""
    src = "SELECT ?l WHERE { ?r ex:reifies <<( ?s ?p ?o )>> ; :label ?l }\n"
    result = format_sparql(src)
    _assert_no_split_artefacts(result)
    assert "<<( ?s ?p ?o )>>" in result
    assert (
        result == "SELECT ?l WHERE {\n"
        "    ?r ex:reifies <<( ?s ?p ?o )>> ;\n"
        "        :label ?l\n"
        "}\n"
    )


def test_reifier_formatting_is_idempotent() -> None:
    """Formatting twice yields the same output (stable fixed point)."""
    src = "ASK { << :s :p :o >> :conf 0.9 . }\n"
    once = format_sparql(src)
    twice = format_sparql(once)
    assert once == twice
    _assert_no_split_artefacts(twice)


def test_triple_term_formatting_is_idempotent() -> None:
    """Formatting the triple-term form twice yields the same output."""
    src = "SELECT ?l WHERE { ?r ex:reifies <<( ?s ?p ?o )>> ; :label ?l }\n"
    once = format_sparql(src)
    twice = format_sparql(once)
    assert once == twice
    _assert_no_split_artefacts(twice)


def test_quoted_triple_as_subject() -> None:
    """A quoted triple in subject position (``<< :s :p :o >> :p2 :o2``)
    formats atomically too."""
    src = "ASK { << :s :p :o >> :p2 :o2 . }\n"
    result = format_sparql(src)
    _assert_no_split_artefacts(result)
    assert "<< :s :p :o >> :p2 :o2" in result


def test_plain_iri_still_formats() -> None:
    """Guard against regression: an ordinary ``<...>`` IRI must still lex as
    a single IRI and format normally now that ``<<`` is special-cased."""
    src = "ASK { <http://example.org/s> :p :o . }\n"
    result = format_sparql(src)
    _assert_no_split_artefacts(result)
    assert "<http://example.org/s> :p :o ." in result
    tokens = tokenize("<http://example.org/s>", sparql_mode=True)
    assert len(tokens) == 1
    assert tokens[0].type == TT.IRI
