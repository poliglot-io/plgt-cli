"""Build-time expansion of ``script://`` URI refs into canonical SPARQL literals.

Authors can write::

    wgt-iss:ListIssuesCommit
        a plgt:UpdateScript ;
        plgt-proc:update <script://scripts/list-issues-commit.rq> .

and keep the SPARQL body in a separate ``.rq`` file. At build time this
module:

1. Walks the merged matrix graph for objects of SPARQL-bearing predicates.
2. For each ``script://`` URI ref, loads the referenced ``.rq`` from the
   matrix directory.
3. Builds a self-contained SPARQL string by prepending only the ``PREFIX``
   declarations actually used in the script body, drawn from the matrix's
   prefix table. Script-local ``PREFIX`` declarations override the
   matrix's for that script.
4. Replaces the original triple with one whose object is a string literal
   holding the assembled SPARQL.

Wire-format output is byte-equivalent to a matrix that authored the SPARQL
inline with the matching prefixes. The runtime never sees ``script://`` —
only inlined canonical TTL.

The matrix prefix table comes from the merged graph's ``namespaces()`` —
every TTL file that's been parsed contributes its bindings.
"""

from __future__ import annotations

import re
from pathlib import Path  # noqa: TC003 — used at runtime via matrix_dir.resolve()
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, URIRef

from plgt.core.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rdflib.namespace import Namespace


SCRIPT_SCHEME = "script://"

# Predicates whose object position accepts SPARQL string content. Extend this
# set when a new SPARQL-bearing predicate is added in the spec; no parser
# changes are needed beyond registering the URI here.
SPARQL_BEARING_PREDICATES: frozenset[URIRef] = frozenset(
    {
        URIRef("https://poliglot.io/os/spec/processes#update"),
        URIRef("https://poliglot.io/os/spec#fromJSON"),
        URIRef("https://poliglot.io/os/spec#fromValue"),
        URIRef("https://poliglot.io/os/spec/processes#json"),
        URIRef("https://poliglot.io/os/spec/iam#sparql"),
    }
)

# Match SPARQL prefixed names: prefix:localName where prefix is empty or
# letters/digits/underscore/hyphen, and localName starts with a letter/_/digit
# and contains letter/digit/_/-/. Run only over "code-only" text — comments
# and string literals are stripped first so a literal like `"wgt-iss:foo"`
# inside a JSON DSL body doesn't trigger spurious PREFIX injection.
#
# The prefix label is OPTIONAL so the default/empty prefix form (`:foo`) is
# captured too — SPARQL allows it and matrices that bind a single
# ``@prefix : <…>`` use it routinely. Group(1) is ``None`` when the empty
# prefix is matched; callers normalise that to ``""``.
_PREFIXED_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_\-:])([A-Za-z][A-Za-z0-9_\-]*)?:[A-Za-z_][A-Za-z0-9_\-.]*"
)

# Match SPARQL PREFIX declarations. Captures the prefix label (group 1).
# Script-local PREFIX declarations shadow the matrix's table for that
# script's resolution. Whitespace between the prefix label and the IRI is
# permitted (including newlines).
_PREFIX_DECL_PATTERN = re.compile(
    r"PREFIX\s+([A-Za-z][A-Za-z0-9_\-]*)\s*:\s*<[^>]+>", re.IGNORECASE
)

# Tokens whose interior must not be scanned for prefix uses, in left-to-right
# priority order. URIs (`<...>`) are matched and kept intact so the regex
# doesn't mistake a `#fragment` inside a URI for the start of a comment.
# Strings and comments are matched and replaced with whitespace. Order
# matters: longer/more-specific patterns first.
_TOKEN_PATTERN = re.compile(
    r"<[^>\s]*>"  # URIs — kept intact
    r'|"""(?:[^"\\]|\\.|"(?!""))*"""'  # triple-double strings
    r"|'''(?:[^'\\]|\\.|'(?!''))*'''"  # triple-single strings
    r'|"(?:[^"\\\n]|\\.)*"'  # double-quoted strings
    r"|'(?:[^'\\\n]|\\.)*'"  # single-quoted strings
    r"|#[^\n]*",  # line comments — must be LAST so URI `#fragment` wins
    re.DOTALL,
)


def _strip_strings_and_comments(script_body: str) -> str:
    """Replace SPARQL string literals and comments with whitespace, leaving
    URI brackets (``<...>``) intact.

    Used to prepare a script body for regex-based prefix discovery: a
    `prefix:localName` substring inside a string literal or comment must
    not trigger PREFIX injection. URIs are not scanned for prefix uses, so
    keeping them intact (rather than stripping) is fine — they don't
    contain SPARQL prefixed names. Crucially, leaving URIs intact prevents
    a `#fragment` inside a URI from being misread as the start of a SPARQL
    line comment.

    Replacement uses spaces (matching the stripped span's length) so line
    and column offsets stay stable for any future diagnostic consumer.
    """

    def replace(match: re.Match) -> str:
        text = match.group(0)
        if text.startswith("<"):
            # URI — keep intact so the leading character of the next token
            # is at the right offset. URIs don't contain SPARQL prefixed
            # names so leaving them in doesn't pollute the prefix scan.
            return text
        return " " * len(text)

    return _TOKEN_PATTERN.sub(replace, script_body)


def expand_script_refs(
    graph: Graph,
    matrix_dir: Path,
    script_origin: dict[tuple, Path] | None = None,
) -> Graph:
    """Expand every ``script://`` URI ref in ``graph`` into a string literal.

    ``matrix_dir`` is the directory containing the matrix's source files; the
    URI's path is resolved relative to it. The function mutates ``graph`` in
    place (rdflib graphs aren't copy-cheap) and returns it for chaining.

    When ``script_origin`` is supplied, populates it with a
    ``{(subject, predicate): script_path}`` map so downstream phases (E0203
    in particular) can pin diagnostics to the ``.rq`` file the body came
    from, not the TTL that referenced it.

    Raises ``ValidationError`` on any of:

    * referenced file is outside ``matrix_dir`` (path-traversal attempt),
    * referenced file doesn't exist,
    * URI scheme is malformed (missing path component).
    """
    prefix_table = dict(graph.namespaces())

    to_replace: list[tuple[URIRef | Namespace, URIRef, URIRef]] = []
    for subject, predicate, obj in graph:
        if predicate not in SPARQL_BEARING_PREDICATES:
            continue
        if not isinstance(obj, URIRef):
            continue
        if not str(obj).startswith(SCRIPT_SCHEME):
            continue
        to_replace.append((subject, predicate, obj))

    for subject, predicate, obj in to_replace:
        script_uri = str(obj)
        script_path = _resolve_script_path(script_uri, matrix_dir)
        script_body = script_path.read_text(encoding="utf-8")
        inlined = inline_with_prefixes(script_body, prefix_table)
        graph.remove((subject, predicate, obj))
        graph.add((subject, predicate, Literal(inlined)))
        if script_origin is not None:
            script_origin[(subject, predicate)] = script_path

    return graph


def _load_script_body(script_uri: str, matrix_dir: Path) -> str:
    """Resolve ``script://path`` to a file under ``matrix_dir`` and return its
    contents. Rejects absolute paths and ``..`` components. Kept for backwards
    compatibility with callers that don't need the resolved Path; new code
    should use ``_resolve_script_path`` + ``Path.read_text`` directly.
    """
    return _resolve_script_path(script_uri, matrix_dir).read_text(encoding="utf-8")


def _resolve_script_path(script_uri: str, matrix_dir: Path) -> Path:
    """Resolve ``script://path`` to a concrete file under ``matrix_dir``,
    enforcing the same safety rules as ``_load_script_body`` (no absolute
    paths, no ``..`` traversal, must end in ``.rq``).
    """
    relative = script_uri[len(SCRIPT_SCHEME) :]
    # Drop optional leading authority (reserved for future use). For now any
    # authority is rejected.
    if relative.startswith("//"):
        msg = f"script:// URI {script_uri!r} has an authority component; not yet supported"
        raise ValidationError(msg)
    if not relative or relative.endswith("/"):
        msg = f"script:// URI {script_uri!r} has no file path"
        raise ValidationError(msg)
    if not relative.endswith(".rq"):
        msg = f"script:// URI {script_uri!r} must reference a .rq file"
        raise ValidationError(msg)

    candidate = (matrix_dir / relative).resolve()
    try:
        candidate.relative_to(matrix_dir.resolve())
    except ValueError as e:
        msg = f"script:// URI {script_uri!r} escapes the matrix directory"
        raise ValidationError(msg) from e

    if not candidate.is_file():
        msg = f"script:// URI {script_uri!r} references missing file {candidate}"
        raise ValidationError(msg)

    return candidate


def inline_with_prefixes(script_body: str, prefix_table: dict[str, URIRef]) -> str:
    """Return a self-contained SPARQL string by prepending matrix prefixes the
    body uses but doesn't already declare itself.

    Script-local PREFIX declarations win; the matrix's table fills in
    references that have no inline declaration. We only emit prefixes the
    body actually uses (parsed via a lightweight prefixed-name regex) so the
    inlined string stays compact and noise-free.
    """
    declared_in_script = _prefixes_already_declared(script_body)
    used_prefixes = _prefixes_referenced(script_body)
    needed = sorted(used_prefixes - declared_in_script)

    declarations: list[str] = []
    for prefix in needed:
        namespace = prefix_table.get(prefix)
        if namespace is None:
            # Unknown prefix — leave it to the SPARQL parser to flag at
            # runtime / validate time. We don't fail the build because a
            # SPARQL keyword could be misclassified by our loose regex.
            continue
        declarations.append(f"PREFIX {prefix}: <{namespace}>")

    if not declarations:
        return script_body

    return "\n".join(declarations) + "\n\n" + script_body


def _prefixes_already_declared(script_body: str) -> set[str]:
    """Find prefixes declared inline in the script body (case-insensitive).

    Runs the full PREFIX-declaration regex over the original body. URIs in
    `PREFIX foo: <https://example#>` need to remain intact for the regex to
    match the trailing `<...>`, so we cannot strip them here. The regex
    itself is specific enough (`PREFIX\\s+name\\s*:\\s*<...>`) that a
    substring inside a string literal is extremely unlikely to falsely
    match — and even if it did, the consequence is suppressing one matrix-
    table injection, which fails loudly at SPARQL parse time when the
    undeclared prefix is used. Acceptable trade-off.
    """
    return {match.group(1) for match in _PREFIX_DECL_PATTERN.finditer(script_body)}


def _prefixes_referenced(script_body: str) -> set[str]:
    """Collect prefix labels used as ``prefix:localName`` in the script body.

    Strips comments and string literals before scanning so prefixed-name-
    shaped substrings inside them don't trigger spurious injection.
    ``PREFIX``-declaration patterns are then removed so the prefixes used
    inside declarations don't double-count as usage.
    """
    code_only = _strip_strings_and_comments(script_body)
    body_without_decls = _PREFIX_DECL_PATTERN.sub("", code_only)
    # `findall` returns the captured group (group 1). With the prefix label
    # now optional in the pattern, that group is ``None`` for the empty/default
    # prefix; normalise to "" so callers can treat it uniformly.
    matches = [m or "" for m in _PREFIXED_NAME_PATTERN.findall(body_without_decls)]
    # Filter out SPARQL keywords that the loose regex might match
    # (e.g. ``a:foo`` — rare but possible). Lowercase keyword check is
    # sufficient for now.
    return {m for m in matches if m.lower() not in _SPARQL_KEYWORDS_LIKE_PREFIXES}


def extract_prefixed_names(script_body: str) -> list[tuple[str, str]]:
    """Public helper: every ``prefix:localName`` occurrence in a SPARQL
    body, in source order with duplicates preserved. Strips comments and
    string literals first.

    Returned as a list of ``(prefix, local_name)`` tuples. The caller is
    responsible for resolving the prefix against a namespace table and
    deciding what to do with unknown prefixes — this function is purely
    syntactic.
    """
    code_only = _strip_strings_and_comments(script_body)
    body_without_decls = _PREFIX_DECL_PATTERN.sub("", code_only)
    out: list[tuple[str, str]] = []
    for match in _PREFIXED_NAME_PATTERN.finditer(body_without_decls):
        # Group(1) is None for the empty/default-prefix form (`:foo`);
        # downstream resolution looks up "" in the prefix table.
        prefix = match.group(1) or ""
        if prefix.lower() in _SPARQL_KEYWORDS_LIKE_PREFIXES:
            continue
        # Whole match is `prefix:localName`; split deterministically.
        full = match.group(0)
        local = full[len(prefix) + 1 :]
        out.append((prefix, local))
    return out


# SPARQL tokens that match the prefix regex but aren't real prefixes. Keep
# small; over-filtering risks dropping a real prefix named like a keyword.
_SPARQL_KEYWORDS_LIKE_PREFIXES: frozenset[str] = frozenset(
    {
        # No SPARQL keywords match the regex shape "letter then word chars
        # then colon then identifier" by accident, since keywords don't end
        # in `:`. Reserved for future false-positive triage.
    }
)


def script_predicates_in_graph(graph: Graph) -> Iterable[URIRef]:
    """Iterate every SPARQL-bearing predicate present in ``graph``.

    Diagnostic helper used by validators that need to enumerate the SPARQL
    bodies in a matrix without re-parsing the predicate set.
    """
    seen: set[URIRef] = set()
    for _, predicate, _ in graph:
        if predicate in SPARQL_BEARING_PREDICATES and predicate not in seen:
            seen.add(predicate)
            yield predicate


__all__ = [
    "SCRIPT_SCHEME",
    "SPARQL_BEARING_PREDICATES",
    "expand_script_refs",
    "inline_with_prefixes",
    "script_predicates_in_graph",
]
