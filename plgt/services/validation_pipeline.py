"""Local validation pipeline.

Runs the same checks the platform runs at install time,
plus checks the platform doesn't easily do (cross-reference integrity
inside SPARQL, import-vs-dependency consistency). Returns a
``DiagnosticBag`` the CLI surfaces as JSON or pretty terminal output.

Phases:

1. TTL parse — per-file syntax errors.
2. SPARQL parse — per-embedded-body syntax errors, including ``.rq`` files
   referenced by ``script://`` URIs.
3. Assemble — merge local matrix TTL + cached deps + cached system matrix
   into a single graph. Expands ``script://`` refs in-place.
4. Cross-reference integrity — every imported URI in ``plgt-mtx:imports``
   resolves to a package declared in ``poliglot.yml`` ``dependencies:``
   (or is the system matrix).
5. Namespace enforcement — every subject triple uses the matrix's
   namespace URI prefix (mirrors backend's namespace check).
6. SHACL — pyshacl over the assembled graph against the system matrix's
   shapes + the local matrix's own shapes.
7. Predicate / class existence — every predicate URI and rdf:type object
   used in local TTL must resolve to a declared term in the assembled
   graph. Unknown terms surface with did-you-mean suggestions from the
   same namespace (``difflib.get_close_matches`` over the term index).
8. GREL function call validation — every ``plgt:executesFunction``
   target must resolve to a declared function. When the function's
   ``fno:expects`` parameter list is known, argument arity is checked.
9. Variable / secret resolution sanity — every ``plgt-build:Variable`` and
   ``plgt-scrt:ManagedSecret`` declaration carries the required metadata
   fields and there are no name conflicts. Unbound variable references at
   activation time are deferred and surfaced as ``PLGT_W0701``
   (informational).

The pipeline is short-circuit-friendly: a failure in phase 1 (TTL parse)
prevents phases that need the parsed graph from running on that file. We
collect diagnostics from every file before short-circuiting, so a single
run reports all parse errors in the project.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import rdflib
from rdflib import Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.plugins.sparql.parser import parseQuery, parseUpdate

from plgt.services.build_service import create_build_config, normalize_path
from plgt.services.deps_install_service import (
    cache_dir_for,
    cache_root_for,
    lockfile_path_for,
)
from plgt.services.deps_lockfile import read_lockfile
from plgt.services.diagnostics import DiagnosticBag, relative_path
from plgt.services.script_expander import (
    SCRIPT_SCHEME,
    SPARQL_BEARING_PREDICATES,
    expand_script_refs,
    extract_prefixed_names,
    inline_with_prefixes,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


PLGT_MTX_IMPORTS = URIRef("https://poliglot.io/os/spec/matrix#imports")
PLGT_MTX_MATRIX = URIRef("https://poliglot.io/os/spec/matrix#Matrix")
PLGT_MTX_DECLARE = URIRef("https://poliglot.io/os/spec/matrix#declare")
PLGT_MTX_PREFIX = URIRef("https://poliglot.io/os/spec/matrix#prefix")
PLGT_MTX_NAMESPACE = URIRef("https://poliglot.io/os/spec/matrix#namespace")
RDF_TYPE = URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type")

PLGT_OS_EXECUTES_FUNCTION = URIRef("https://poliglot.io/os/spec#executesFunction")
FNO_EXPECTS = URIRef("https://w3id.org/function/ontology#expects")

PLGT_BUILD_VARIABLE = URIRef("https://poliglot.io/os/spec/build#Variable")
PLGT_BUILD_VARIABLE_TYPE = URIRef("https://poliglot.io/os/spec/build#variableType")
PLGT_BUILD_LABEL = URIRef("https://poliglot.io/os/spec/build#label")
PLGT_BUILD_DESCRIPTION = URIRef("https://poliglot.io/os/spec/build#description")
PLGT_BUILD_REQUIRED = URIRef("https://poliglot.io/os/spec/build#required")
PLGT_SCRT_MANAGED_SECRET = URIRef("https://poliglot.io/os/spec/secrets#ManagedSecret")
PLGT_SCRT_DESCRIPTION = URIRef("https://poliglot.io/os/spec/secrets#description")

# Custom SHACL target type that selects every IRI-identified (non-blank,
# non-literal) node in the data graph as a focus node. Standard SHACL engines
# (pyshacl) do not know this target and raise a ShapeLoadError on it, so we
# STRIP every NodeShape that carries it before handing the graph to pyshacl and
# evaluate those shapes natively in Python (see _evaluate_iri_node_targets).
PLGT_IRI_NODE_TARGET = URIRef("https://poliglot.io/os/spec#IRINodeTarget")

SH_NS = rdflib.Namespace("http://www.w3.org/ns/shacl#")


@dataclass
class ValidationResult:
    """Outcome of running the pipeline. ``assembled`` is the merged graph
    after dep loading + script:// expansion, useful for schema-query
    commands that piggyback on the same pipeline.
    """

    diagnostics: DiagnosticBag
    assembled: Graph | None


def validate_project(
    project_dir: Path, *, workspace: str | None = None
) -> ValidationResult:
    """Run the validation pipeline against ``project_dir``.

    The project must already have its dependencies installed via
    ``plgt sync``; otherwise phase 4+ won't have access to imported
    matrices and will surface a precondition error.

    ``workspace`` selects which per-mode lockfile and dep cache subtree to
    read: a slug picks ``.matrix/deps/<slug>.lock`` and
    ``.matrix/deps/<slug>/`` (workspace-sync mode), and ``None`` picks
    ``.matrix/deps/_registry.lock`` and ``.matrix/deps/_registry/``
    (registry-resolve mode). The CLI's `validate` command threads its
    `--from-workspace` / `--from-registry` flags (or the default-workspace
    fallback) into this parameter so validation always reflects the same
    resolution that the matching install populated.
    """
    bag = DiagnosticBag()
    project_dir = project_dir.resolve()

    # Phase 0: locate poliglot.yml. Without it there's nothing to validate.
    config_path = project_dir / "poliglot.yml"
    if not config_path.exists():
        bag.error(
            "PLGT_E0900",
            f"No poliglot.yml found in {project_dir}",
            path=str(project_dir),
        )
        return ValidationResult(diagnostics=bag, assembled=None)

    # Phase 0: load the lockfile for the active mode. Without it the deps cache hasn't been
    # populated — phases that need the assembled graph will fail.
    lockfile_path = lockfile_path_for(project_dir, workspace)
    lockfile = read_lockfile(lockfile_path)
    if lockfile is None:
        rel_lockfile = lockfile_path.relative_to(project_dir)
        hint = (
            f"`plgt sync --from-workspace {workspace}`"
            if workspace
            else "`plgt sync --from-registry`"
        )
        bag.error(
            "PLGT_E0900",
            f"No {rel_lockfile} found. Run {hint} to populate the dependency cache "
            "before validating.",
            path=str(rel_lockfile),
        )
        return ValidationResult(diagnostics=bag, assembled=None)

    # Phase 1+2+3: parse + assemble per-matrix TTL.
    # The project can declare multiple matrices in poliglot.yml; each has its
    # own dir + spec patterns. Walk every matrix's spec dirs and merge into
    # a single local graph (validation considers the whole project at once).
    try:
        package_config = create_build_config(config_path)
    except Exception as e:  # noqa: BLE001 — BuildError or yaml errors
        bag.error("PLGT_E0900", f"Failed to parse poliglot.yml: {e}")
        return ValidationResult(diagnostics=bag, assembled=None)

    matrix_specs: list[tuple[Path, Path]] = []  # (matrix_dir, spec_dir)
    for matrix_cfg in package_config.matrices:
        matrix_dir = project_dir / matrix_cfg.path
        if not matrix_dir.is_dir():
            bag.warning(
                "PLGT_W0001",
                f"Matrix directory missing: {matrix_dir}",
            )
            continue
        # Primary spec dir is the first spec pattern that exists.
        for pattern in matrix_cfg.spec_patterns:
            candidate = matrix_dir / normalize_path(pattern)
            if candidate.is_dir():
                matrix_specs.append((matrix_dir, candidate))
                break

    if not matrix_specs:
        bag.warning(
            "PLGT_W0001",
            f"No matrices found under {project_dir}; nothing to validate locally.",
        )
        return ValidationResult(diagnostics=bag, assembled=None)

    # Parse each matrix into its own graph and expand that matrix's
    # script:// refs against its own spec_dir. Merging happens AFTER expansion
    # so a script URI authored in one matrix never gets resolved against
    # another matrix's spec/ directory. `file_origin` tracks which TTL each
    # triple came from so later phases can attach `path` to their diagnostics.
    # `script_origin` records the resolved .rq path for each (subject, predicate)
    # pair whose body came from a script:// URI; E0203 uses this to pin
    # SPARQL-body diagnostics at the .rq file (not the TTL that references it).
    local_graph = Graph()
    file_origin: dict[tuple, str] = {}
    script_origin: dict[tuple, str] = {}
    parsed_any = False
    for _matrix_dir, spec_dir in matrix_specs:
        matrix_graph = Graph()
        if not _parse_local_ttl_into(
            spec_dir, project_dir, matrix_graph, bag, file_origin=file_origin
        ):
            continue
        parsed_any = True
        _check_sparql_syntax(matrix_graph, spec_dir, project_dir, bag)
        _check_prefix_consistency(matrix_graph, spec_dir, project_dir, bag)
        try:
            matrix_script_paths: dict[tuple, Path] = {}
            expand_script_refs(
                matrix_graph, spec_dir, script_origin=matrix_script_paths
            )
            for key, path in matrix_script_paths.items():
                script_origin[key] = relative_path(path, project_dir)
        except Exception as e:  # noqa: BLE001 — surfaces from ValidationError
            bag.error(
                "PLGT_E0800",
                f"script:// expansion failed: {e}",
                path=relative_path(spec_dir, project_dir),
            )
            return ValidationResult(diagnostics=bag, assembled=None)
        # Merge with prefix-table preservation: copy namespaces first so the
        # final local_graph still has every matrix's declared prefixes.
        for prefix, namespace in matrix_graph.namespaces():
            local_graph.bind(prefix, namespace, replace=False)
        local_graph += matrix_graph
    if not parsed_any:
        return ValidationResult(diagnostics=bag, assembled=None)

    # Phase 5 prep: collect local matrix namespaces — anything declared
    # `a plgt-mtx:Matrix` in any of the project's TTL files. Used both
    # for namespace enforcement (phase 5) and to allow same-package
    # sibling imports through phase 4 without requiring them to appear
    # in the lockfile.
    matrix_namespaces = _extract_matrix_namespaces(local_graph)

    # Phase 4: import-vs-dependency consistency. Imports may resolve to
    # (a) a sibling matrix in this project, (b) the system matrix, or
    # (c) a package declared in the lockfile's `dependencies:`.
    declared_packages: set[tuple[str, str]] = {
        (d.publisher, d.name) for d in lockfile.dependencies
    }
    declared_packages.add(
        (lockfile.engine.publisher, lockfile.engine.name)
    )  # the system matrix is always implicitly declared via engineVersion.
    _check_imports_against_declared(
        local_graph,
        declared_packages,
        matrix_namespaces,
        project_dir,
        workspace,
        bag,
        file_origin=file_origin,
    )

    # Subject → file index, built once and shared by phases that need to
    # pin a diagnostic to "the TTL that declared this RDF subject" (phase 5
    # namespace, phase 6 SHACL, phase 9 variables/secrets). Without this,
    # SHACL findings — which only know the focus node — would drop into
    # the graph-level bucket because rdflib has no native triple-origin.
    subject_origin = _build_subject_origin(file_origin)

    # Phase 5: namespace enforcement: uses matrix_namespaces collected above.
    _check_namespace_enforcement(
        local_graph, matrix_namespaces, project_dir, bag, subject_origin=subject_origin
    )

    # Phase 3: assemble (local + cached deps + system matrix).
    assembled = _load_assembled_graph(
        project_dir, workspace, lockfile, local_graph, bag
    )

    # Phase 6: SHACL validation. Shapes + the class hierarchy needed for RDFS
    # inference come from the assembled graph (package + deps + system matrix),
    # but the DATA pyshacl walks is scoped to the package's OWN resources
    # (``local_graph``): the dependencies were already validated when published,
    # so re-validating their resources here is wasted work — and over a large
    # transitive closure (e.g. the system matrix) it dominates the run.
    if assembled is not None:
        _run_shacl_validation(
            assembled,
            project_dir,
            bag,
            local_graph=local_graph,
            local_namespaces=matrix_namespaces,
            subject_origin=subject_origin,
        )

    # Phases 7-9 need the assembled graph; skip when assembly failed (any diagnostics from
    # those phases would point at terms the user can't possibly resolve without the dep
    # graph, so they would be noise).
    if assembled is not None:
        # Phase 7: predicate / class existence with did-you-mean.
        _check_predicate_class_existence(
            local_graph=local_graph,
            assembled=assembled,
            bag=bag,
            file_origin=file_origin,
            script_origin=script_origin,
            project_dir=project_dir,
        )
        # Phase 8: GREL function call validation.
        _check_grel_function_calls(
            local_graph=local_graph,
            assembled=assembled,
            bag=bag,
            file_origin=file_origin,
            project_dir=project_dir,
        )
        # Phase 9: variable / secret resolution sanity.
        _check_variables_and_secrets(
            local_graph=local_graph, bag=bag, subject_origin=subject_origin
        )

    return ValidationResult(diagnostics=bag, assembled=assembled)


def _build_subject_origin(
    file_origin: dict[tuple, str] | None,
) -> dict[URIRef, str]:
    """Invert ``file_origin`` (triple → path) into a (subject → path) map,
    first-writer wins. Used by phases that only know the offending subject
    URI and need to pin their diagnostic to the TTL that declared it.
    """
    out: dict[URIRef, str] = {}
    if file_origin:
        for (s_key, _p, _o), p_path in file_origin.items():
            if isinstance(s_key, URIRef):
                out.setdefault(s_key, p_path)
    return out


# ---------------------------------------------------------------------------
# Phase 1+2: TTL parse + per-file diagnostics


def _parse_local_ttl_into(
    spec_dir: Path,
    project_dir: Path,
    graph: Graph,
    bag: DiagnosticBag,
    file_origin: dict[tuple, str] | None = None,
) -> bool:
    """Parse every TTL file under ``spec_dir`` into ``graph``. Surfaces a
    parse-error diagnostic per failing file. Returns True if at least one
    file parsed cleanly.

    When ``file_origin`` is supplied, populates it with a ``{(s, p, o): path}``
    map so downstream diagnostics (PLGT_E0201/E0202/etc.) can point at the
    TTL file that authored each triple. rdflib doesn't track triple origin
    natively, so we parse each file into a scratch graph and stash the
    mapping before merging into the project-wide graph.
    """
    parsed_any = False

    for ttl_path in sorted(spec_dir.rglob("*.ttl")):
        try:
            if file_origin is None:
                graph.parse(source=str(ttl_path), format="turtle")
            else:
                scratch = Graph()
                scratch.parse(source=str(ttl_path), format="turtle")
                rel = relative_path(ttl_path, project_dir)
                for triple in scratch:
                    # First-writer wins so duplicate triples across files stay
                    # pinned to wherever they appeared first.
                    file_origin.setdefault(triple, rel)
                for prefix, ns in scratch.namespaces():
                    graph.bind(prefix, ns, replace=False)
                graph += scratch
            parsed_any = True
        except rdflib.exceptions.ParserError as e:
            line, col = _extract_parse_location(e)
            bag.error(
                "PLGT_E0001",
                f"TTL parse error: {e}",
                path=relative_path(ttl_path, project_dir),
                line=line,
                col=col,
            )
        except Exception as e:  # noqa: BLE001 — rdflib raises many shapes
            line, col = _extract_parse_location(e)
            bag.error(
                "PLGT_E0001",
                f"TTL parse error: {e}",
                path=relative_path(ttl_path, project_dir),
                line=line,
                col=col,
            )

    return parsed_any


def _extract_parse_location(error: Exception) -> tuple[int | None, int | None]:
    """Best-effort extraction of line/col from an rdflib parse error.

    Prefers structured attributes when the exception exposes them
    (rdflib's ``BadSyntax`` carries ``lines`` from the notation3 parser).
    Falls back to scraping the message because not every parser exception
    shape has the attributes, and the message format is stable across
    recent rdflib versions.
    """
    # Try structured attributes first. notation3's BadSyntax sets `lines` as the count of
    # completed newlines before the error, so the human-readable line number is `lines + 1`
    # (matches what BadSyntax's own __str__ prints).
    structured_lines = getattr(error, "lines", None)
    if isinstance(structured_lines, int) and structured_lines >= 0:
        return structured_lines + 1, None
    # Fall back to message regex. Anchor to "at line N" so a URI happening to contain
    # "line5.ttl" doesn't get mis-parsed as the line number.

    message = str(error)
    line: int | None = None
    col: int | None = None
    line_match = re.search(r"at\s+line\s+(\d+)", message, re.IGNORECASE)
    if line_match:
        try:
            line = int(line_match.group(1))
        except ValueError:
            line = None
    col_match = re.search(r"col(?:umn)?\s+(\d+)", message, re.IGNORECASE)
    if col_match:
        try:
            col = int(col_match.group(1))
        except ValueError:
            col = None
    return line, col


# ---------------------------------------------------------------------------
# SPARQL syntax check


def _check_sparql_syntax(
    graph: Graph, spec_dir: Path, project_dir: Path, bag: DiagnosticBag
) -> None:
    """Parse every embedded SPARQL body and every .rq file referenced by
    ``script://`` for syntax errors. Emits PLGT_E0100 per failure.

    Two-phase strategy: first the literal string-body case (already inline
    in TTL), then the script:// case (load file, parse standalone). The
    assembled body for script:// cases is what the server will see after
    expansion, so we parse with the matrix prefixes injected.

    ``spec_dir`` is the base for ``script://`` resolution; ``project_dir``
    is used for diagnostic path normalisation.
    """
    prefix_table = dict(graph.namespaces())

    def _named_ancestor(node: object) -> str:
        """Walk inbound triples until we hit a non-blank subject. The TTL
        author wrote `plgt:fromJSON` inside a chain of bracketed
        anonymous nodes hanging off a named Action — surfacing the named
        ancestor in the warning lets them locate the body without grepping
        blank-node IDs (which are unstable across parses).
        """
        seen: set[str] = set()
        cur = node
        while not isinstance(cur, URIRef):
            key = str(cur)
            if key in seen:
                break
            seen.add(key)
            parent = next(graph.subjects(object=cur), None)
            if parent is None:
                break
            cur = parent
        return str(cur)

    for subject, predicate, obj in graph:
        if predicate not in SPARQL_BEARING_PREDICATES:
            continue

        if isinstance(obj, URIRef) and str(obj).startswith(SCRIPT_SCHEME):
            relative = str(obj)[len(SCRIPT_SCHEME) :]
            script_path = spec_dir / relative
            if not script_path.is_file():
                # Missing-file errors are already surfaced by
                # expand_script_refs at phase 3; don't double-report here.
                continue
            try:
                body = script_path.read_text(encoding="utf-8")
            except OSError as e:
                bag.error(
                    "PLGT_E0100",
                    f"Could not read SPARQL script: {e}",
                    path=relative_path(script_path, project_dir),
                )
                continue
            body = inline_with_prefixes(body, prefix_table)
            source_path = relative_path(script_path, project_dir)
        elif isinstance(obj, Literal):
            body = str(obj)
            source_path = None
        else:
            continue

        owner = _named_ancestor(subject)
        _parse_sparql_body(body, predicate, owner, source_path, bag)


def _parse_sparql_body(
    body: str,
    predicate: URIRef,
    subject_uri: str,
    source_path: str | None,
    bag: DiagnosticBag,
) -> None:
    """Try parsing ``body`` as SPARQL. Use parseUpdate for update predicates,
    parseQuery for the rest. The JSON DSL and DEFINE DSL aren't standard
    SPARQL and route to a separate parser; this function surfaces a
    ``PLGT_W0102`` informational warning so the skip is visible rather
    than silent. ``subject_uri`` identifies which resource carries the body
    so authors can locate skipped bodies without a per-file line number.
    """
    pred_str = str(predicate)
    is_update = pred_str.endswith("#update")
    # Both plgt:fromJSON (action property function output) and plgt-proc:json
    # (Script/Query content) carry JSON DSL bodies. Route both to the bundled
    # parser instead of falling through to rdflib's SPARQL parser, which rejects
    # "JSON {" as not-a-SelectQuery.
    is_json_dsl = pred_str.endswith(("#fromJSON", "#json"))
    if is_json_dsl:
        # Lightweight structural validator — verifies the PREFIX/JSON/WHERE
        # frame, brace balance, and variable scoping. The full recursive-descent
        # parser lives server-side; the platform re-checks at install time,
        # but catching the common authoring errors here keeps the loop tight.
        # Unknown prefixed names inside the body are already covered by the
        # phase-7 E0203 walk.
        _validate_json_dsl_body(body, subject_uri, source_path, bag)
        return
    if _is_define_dsl(body):
        bag.warning(
            "PLGT_W0102",
            f"DEFINE DSL body on <{subject_uri}> skipped: the DEFINE DSL parser "
            "is not yet ported to the CLI. The platform validates this at install time.",
            path=source_path,
            subject=subject_uri,
        )
        return
    try:
        if is_update:
            parseUpdate(body)
        else:
            parseQuery(body)
    except Exception as e:  # noqa: BLE001 — pyparsing raises many shapes
        # pyparsing's ParseException carries `lineno` and `col` attributes when available;
        # other exception shapes don't. Capture them when present so the diagnostic points
        # at the offending position rather than just the file.
        line = getattr(e, "lineno", None)
        col = getattr(e, "col", None)
        bag.error(
            "PLGT_E0100",
            f"SPARQL parse error: {e}",
            path=source_path,
            line=line if isinstance(line, int) else None,
            col=col if isinstance(col, int) else None,
            subject=subject_uri,
        )


def _is_define_dsl(body: str) -> bool:
    """Detect the DEFINE DSL by looking for the `DEFINE` keyword as the
    first non-comment, non-PREFIX line. Bare-form `DEFINE plgt:Search`
    and WHERE-form `DEFINE ?var WHERE { ... }` both start with `DEFINE`.
    """
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith(("PREFIX", "BASE")):
            continue
        return upper.startswith("DEFINE")
    return False


# Variables injected into every JSON DSL evaluation. Authors don't bind these
# in WHERE; they're implicit. Mirrors the system matrix's docs at plgt-proc:Script
# ("Every script evaluates with these standard bindings…").
_JSON_DSL_IMPLICIT_VARS = frozenset({"_process", "_metadata", "_stale"})


def _validate_json_dsl_body(
    body: str, subject_uri: str, source_path: str | None, bag: DiagnosticBag
) -> None:
    """Validate a JSON DSL body using the bundled parser.

    The Python parser at ``plgt.services.json_dsl_parser`` mirrors the
    authoritative server-side grammar. Any deviation from the grammar
    surfaces as ``PLGT_E0103`` with line/col pointing at the offending
    position. The platform re-validates server-side at install time;
    running the same checks locally just shortens the feedback loop.

    Variable scoping (``PLGT_E0104``) is layered on top: the parser
    collects ``?vars`` separately for the value section and any WHERE
    clauses, and value-side references that aren't bound in WHERE — and
    aren't one of the implicit bindings — are flagged.

    Unknown prefixed names inside the body are covered by phase 7
    (``PLGT_E0203``), not here.
    """
    from plgt.services.json_dsl_parser import JsonDslParseError, parse

    try:
        result = parse(body)
    except JsonDslParseError as e:
        bag.error(
            "PLGT_E0103",
            f"JSON DSL body on <{subject_uri}>: {e.message}",
            path=source_path,
            line=e.line,
            col=e.column,
            subject=subject_uri,
        )
        return

    unbound = result.value_vars - result.where_vars - _JSON_DSL_IMPLICIT_VARS
    for var in sorted(unbound):
        bag.error(
            "PLGT_E0104",
            f"Variable `?{var}` in JSON DSL body on <{subject_uri}> is not bound "
            f"in the WHERE clause.",
            path=source_path,
            subject=subject_uri,
        )


# ---------------------------------------------------------------------------
# Phase 4: imports-vs-deps consistency


def _check_imports_against_declared(
    graph: Graph,
    declared_packages: set[tuple[str, str]],
    local_matrix_namespaces: set[str],
    project_dir: Path,
    workspace: str | None,
    bag: DiagnosticBag,
    *,
    file_origin: dict[tuple, str] | None = None,
) -> None:
    """Every plgt-mtx:imports URI must resolve to one of:

    * a sibling matrix in the local project (same-package imports, common
      for multi-matrix packages like `widget/widget` which has
      `widget-core`, `widget-issues`, etc.),
    * the system matrix (always implicitly available via `engineVersion`),
    * a package declared in `poliglot.yml`'s `dependencies:` (looked up
      via the local deps cache).

    A URI that resolves to none of those errors with PLGT_E0401 and a
    suggested `plgt add <publisher>/<name>` fix command.
    """
    deps_cache = cache_root_for(project_dir, workspace)
    if not deps_cache.is_dir():
        # The lockfile precondition check above already covered this case.
        return

    uri_to_package = _build_uri_to_package_index(deps_cache)

    for subject, predicate, import_uri in graph.triples((None, PLGT_MTX_IMPORTS, None)):
        if not isinstance(import_uri, URIRef):
            continue
        uri_str = str(import_uri)
        if uri_str in _STANDARD_VOCABULARIES:
            continue
        # Same-package sibling: a matrix declared in this project's TTLs
        # is implicitly resolvable, no `dependencies:` entry required.
        if uri_str in local_matrix_namespaces:
            continue
        import_path = (
            file_origin.get((subject, predicate, import_uri)) if file_origin else None
        )
        import_subject = str(subject) if isinstance(subject, URIRef) else None
        package = uri_to_package.get(uri_str)
        if package is None:
            bag.error(
                "PLGT_E0401",
                f"Import <{uri_str}> doesn't resolve to any installed dependency. "
                f"Run `plgt add <publisher>/<name>` to declare the providing package.",
                path=import_path,
                subject=import_subject,
            )
            continue
        if package not in declared_packages:
            publisher, name = package
            bag.error(
                "PLGT_E0401",
                f"Import <{uri_str}> belongs to {publisher}/{name}, which is "
                f"not declared in poliglot.yml's `dependencies:`. "
                f"Run `plgt add {publisher}/{name}`.",
                suggest=f"plgt add {publisher}/{name}",
                path=import_path,
                subject=import_subject,
            )


_STANDARD_VOCABULARIES = frozenset(
    {
        # Common third-party / W3C vocabularies that are part of the spec
        # ecosystem but not themselves matrices. Imports of these are
        # harmless and don't need a `dependencies:` entry.
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2004/02/skos/core#",
        "http://www.w3.org/ns/shacl#",
        "http://www.w3.org/2001/XMLSchema#",
        "http://purl.org/dc/terms/",
        "https://ekgf.github.io/dprod/",
    }
)


def _build_uri_to_package_index(
    deps_cache: Path,
) -> dict[str, tuple[str, str]]:
    """Walk every installed package under .matrix/deps/ and index the
    namespace URIs it claims. URI -> (publisher, name).

    A package's matrix.ttl files declare ``X a plgt-mtx:Matrix`` where X
    is the matrix namespace URI. Build an index from those declarations.
    """
    index: dict[str, tuple[str, str]] = {}
    for publisher_dir in deps_cache.iterdir():
        if not publisher_dir.is_dir():
            continue
        for name_dir in publisher_dir.iterdir():
            if not name_dir.is_dir():
                continue
            for version_dir in name_dir.iterdir():
                if not version_dir.is_dir():
                    continue
                pkg_graph = Graph()
                for ttl_path in version_dir.rglob("*.ttl"):
                    try:
                        pkg_graph.parse(source=str(ttl_path), format="turtle")
                    except Exception as e:  # noqa: BLE001 — cache hygiene only
                        logger.debug(
                            "Skipping unparseable cached TTL %s: %s",
                            ttl_path,
                            e,
                        )
                        continue
                for subject in pkg_graph.subjects(RDF_TYPE, PLGT_MTX_MATRIX):
                    index[str(subject)] = (publisher_dir.name, name_dir.name)
    return index


# ---------------------------------------------------------------------------
# Phase 5: namespace enforcement


def _extract_matrix_namespaces(graph: Graph) -> set[str]:
    """Find every ``X a plgt-mtx:Matrix`` subject — those URIs ARE the
    matrix namespaces local matrices may write into. Multiple matrices in
    one project are allowed; the union is the legal write surface.
    """
    return {str(subject) for subject in graph.subjects(RDF_TYPE, PLGT_MTX_MATRIX)}


def _check_prefix_consistency(
    matrix_graph: Graph,
    spec_dir: Path,
    project_dir: Path,
    bag: DiagnosticBag,
) -> None:
    """Every ``@prefix`` declaration in the matrix's TTL that maps to a
    namespace the matrix has declared canonically (via ``plgt-mtx:declare``)
    must use the canonical prefix.

    Catches drift like declaring canonical ``plgt-iam`` for namespace
    ``https://poliglot.io/os/spec/iam#`` but writing ``@prefix iam:`` in
    the source — silent at runtime today, but causes inconsistent qname
    rendering and conflicting entries in the workspace prefix table.

    We re-parse each TTL into a scratch graph because rdflib's merged
    ``Graph.namespaces()`` keeps only one prefix per namespace after binds
    collide — which means a single drifted ``@prefix`` line in one file can
    be silently swallowed by another file's canonical binding.
    """
    canonical_by_namespace: dict[str, str] = {}
    for matrix in matrix_graph.subjects(RDF_TYPE, PLGT_MTX_MATRIX):
        for decl in matrix_graph.objects(matrix, PLGT_MTX_DECLARE):
            prefix_lit = next(matrix_graph.objects(decl, PLGT_MTX_PREFIX), None)
            ns_lit = next(matrix_graph.objects(decl, PLGT_MTX_NAMESPACE), None)
            if prefix_lit is None or ns_lit is None:
                continue
            canonical_by_namespace[str(ns_lit)] = str(prefix_lit)

    if not canonical_by_namespace:
        return

    reported: set[tuple[str, str, str]] = set()
    for ttl_path in sorted(spec_dir.rglob("*.ttl")):
        scratch = Graph()
        try:
            scratch.parse(source=str(ttl_path), format="turtle")
        except Exception:  # noqa: BLE001, S112 — already surfaced by phase 1 parse
            continue
        for prefix, namespace in scratch.namespaces():
            canonical = canonical_by_namespace.get(str(namespace))
            if canonical is None or canonical == prefix:
                continue
            key = (relative_path(ttl_path, project_dir), prefix, str(namespace))
            if key in reported:
                continue
            reported.add(key)
            bag.error(
                "PLGT_E0302",
                (
                    f"@prefix '{prefix}:' maps to namespace <{namespace}> but the "
                    f"matrix's canonical prefix is '{canonical}'. Rename the @prefix "
                    f"declaration to '{canonical}:' so the matrix and its install "
                    "bundle stay internally consistent."
                ),
                path=relative_path(ttl_path, project_dir),
            )


def _check_namespace_enforcement(
    graph: Graph,
    matrix_namespaces: set[str],
    project_dir: Path,
    bag: DiagnosticBag,
    *,
    subject_origin: dict[URIRef, str] | None = None,
) -> None:
    """Every subject triple in the LOCAL graph must use a URI that starts
    with one of the matrix's declared namespaces. Mirrors the server-side
    namespace-rule enforcement.

    Blank nodes are exempt — they're transient and not subject to the
    namespace contract. Subjects in imported (deps) graphs are exempt
    because they belong to other matrices; this check only applies to
    the LOCAL graph passed in.
    """
    if not matrix_namespaces:
        return

    seen_violations: set[str] = set()
    for subject in graph.subjects():
        if not isinstance(subject, URIRef):
            continue
        subject_str = str(subject)
        if any(subject_str.startswith(ns) for ns in matrix_namespaces):
            continue
        # Tolerate imported / W3C vocabulary subjects — they appear because
        # the merged graph includes type-of triples whose subject is the
        # local matrix but those aren't violations.
        # Heuristic: a subject whose URI doesn't match ANY matrix
        # namespace is only a violation if the matrix wrote about it (the
        # local matrix's TTL had it as a subject). We can't easily know
        # that without per-file tracking — for the first cut, we require
        # the subject to either match a namespace OR be a known
        # vocabulary. Refine when per-file source tracking lands.
        if any(subject_str.startswith(v) for v in _STANDARD_VOCABULARIES):
            continue
        if subject_str in seen_violations:
            continue
        seen_violations.add(subject_str)
        path = subject_origin.get(subject) if subject_origin else None
        bag.error(
            "PLGT_E0301",
            f"Subject <{subject_str}> is outside any declared matrix "
            "namespace. Every triple's subject must use one of: "
            + ", ".join(f"<{ns}>" for ns in sorted(matrix_namespaces)),
            subject=subject_str,
            path=path,
        )


# ---------------------------------------------------------------------------
# Phase 3 finished: assemble graph (local + deps + system matrix)


def _load_assembled_graph(
    project_dir: Path,
    workspace: str | None,
    lockfile,
    local_graph: Graph,
    bag: DiagnosticBag,
) -> Graph | None:
    """Merge ``local_graph`` with every cached dep + system matrix. Returns
    the assembled graph. Failures load partial state and surface
    diagnostics for the missing pieces.
    """
    assembled = Graph()
    # Re-add namespaces from local so the assembled graph's prefix table
    # is consistent for downstream queries.
    for prefix, namespace in local_graph.namespaces():
        assembled.bind(prefix, namespace, override=False)
    for triple in local_graph:
        assembled.add(triple)

    # System matrix first, then user deps.
    system = lockfile.engine
    _merge_cache_dir(
        project_dir,
        workspace,
        system.publisher,
        system.name,
        system.version,
        assembled,
        bag,
    )
    for dep in lockfile.dependencies:
        _merge_cache_dir(
            project_dir,
            workspace,
            dep.publisher,
            dep.name,
            dep.version,
            assembled,
            bag,
        )

    return assembled


def _merge_cache_dir(
    project_dir: Path,
    workspace: str | None,
    publisher: str,
    name: str,
    version: str,
    target: Graph,
    bag: DiagnosticBag,
) -> None:
    """Parse every .ttl file under the cached package's spec/ and merge into
    target. The runtime never directly merges from disk like this (it
    fetches assemblies from the platform) so a slight divergence (different
    file ordering, ignored bundled artifacts) is acceptable for local
    validation.
    """
    pkg_dir = cache_dir_for(project_dir, publisher, name, version, workspace=workspace)
    if not pkg_dir.is_dir():
        bag.warning(
            "PLGT_W0901",
            f"Cached package directory missing: {publisher}/{name}@{version}. "
            "Run `plgt sync` to repopulate the cache.",
        )
        return
    for ttl_path in pkg_dir.rglob("*.ttl"):
        try:
            target.parse(source=str(ttl_path), format="turtle")
        except Exception as e:  # noqa: BLE001 — cache parse should not break validation
            bag.warning(
                "PLGT_W0001",
                f"Dependency parse error in {publisher}/{name}@{version}: {e}",
                path=relative_path(ttl_path, project_dir),
            )


# ---------------------------------------------------------------------------
# Phase 6: SHACL


@dataclass(frozen=True)
class _IRINodeConstraint:
    """One ``sh:property`` constraint of a ``plgt:IRINodeTarget`` NodeShape,
    extracted for native evaluation over every named IRI node.

    These advisory shapes are minCount-only documentation nudges: for each
    named IRI focus node we count its values for ``path``; when that count is
    below ``min_count`` we emit a finding carrying ``severity`` and
    ``message``, sourced at ``source_shape``. Every field is read from the
    shapes graph — nothing is hardcoded.
    """

    path: URIRef
    min_count: int
    severity: URIRef | None
    message: str | None
    source_shape: str | None


def _run_shacl_validation(
    graph: Graph,
    project_dir: Path,
    bag: DiagnosticBag,
    *,
    local_graph: Graph | None = None,
    local_namespaces: set[str] | None = None,
    subject_origin: dict[URIRef, str] | None = None,
) -> None:
    """Run SHACL validation, scoped to the package's OWN resources.

    ``graph`` is the assembled graph (package + deps + system matrix); it
    carries every shape and the full class hierarchy RDFS inference needs.
    ``local_graph`` is the package's own TTL (post script:// expansion). When
    supplied, it is what pyshacl actually walks as the data graph, and what the
    native IRI-node pass iterates — so the (already-published) dependency
    resources are not re-validated. When ``None`` (legacy unit-test callers)
    the assembled graph is used as the data graph, preserving old behavior.

    Two evaluation paths:

    1. **Native IRI-node pass.** NodeShapes whose ``sh:target`` object is typed
       ``plgt:IRINodeTarget`` are opaque to pyshacl (ShapeLoadError). We strip
       them from the graph pyshacl sees and evaluate them in Python: for each
       named IRI node we count values for each constraint's ``sh:path`` and emit
       a finding (carrying ``sh:severity`` / ``sh:message``) when below
       ``sh:minCount``.
    2. **pyshacl** over the remaining (standard) shapes.

    Diagnostics map to PLGT_E0500-band codes. pyshacl is a hard dependency;
    import failure or a validator crash surfaces as a real PLGT_E0500 so a clean
    `plgt validate` cannot lie about whether shapes ran.
    """
    data_graph = local_graph if local_graph is not None else graph
    try:
        import pyshacl

        # CLI-only authoring-guidance shapes (resource-annotation suggestions
        # etc.) are bundled with the CLI rather than published in the plgt
        # matrix: they're noise in platform-side validation but useful here.
        _merge_cli_quality_shapes(graph)

        # Carve out the plgt:IRINodeTarget NodeShapes BEFORE pyshacl sees the
        # graph: the custom target is opaque to standard SHACL engines and
        # raises ShapeLoadError. We extract their property constraints for the
        # native pass and strip their subgraphs so pyshacl never loads them.
        iri_node_constraints = _extract_and_strip_iri_node_shapes(graph)

        # Native IRI-node pass: O(1) isURI test per node, no full-graph scan.
        # Iterate only the data graph's own IRI subjects so dependency
        # resources are never reported.
        _evaluate_iri_node_targets(
            data_graph,
            iri_node_constraints,
            bag,
            local_namespaces=local_namespaces,
            subject_origin=subject_origin,
        )

        # pyshacl over the standard shapes. When a scoped local data graph is
        # supplied, shacl_graph carries the assembled shapes + class hierarchy
        # while data_graph holds only the package's own resources, so pyshacl
        # walks just those (deps were validated when published).
        #
        # No RDFS inference. The system matrix already publishes its hierarchy
        # in materialized form, so the types the shapes target are present
        # without inference. Crucially, ``ont_graph`` is NOT passed: handing
        # pyshacl the assembled graph as an ontology forces it to RDFS-infer
        # over the entire transitive dependency closure (tens of thousands of
        # triples with deep class hierarchies) — the operation that made this
        # pass take many minutes. Inference also makes every node an
        # rdfs:Resource, which would fire the targetClass-rdfs:Resource quality
        # shape on thousands of nodes (pure noise). The legacy-caller path
        # (no scoping graph) passes shacl_graph=None so pyshacl extracts shapes
        # from data_graph directly, with no self-union doubling.
        scoped = local_graph is not None
        conforms, report_graph, _ = pyshacl.validate(
            data_graph=data_graph,
            shacl_graph=graph if scoped else None,
            inference="none",
            advanced=True,
            inplace=False,
        )
    except Exception as e:  # noqa: BLE001 — pyshacl + its rdflib internals raise many shapes
        bag.error(
            "PLGT_E0500",
            f"SHACL validation could not run: {type(e).__name__}: {e}",
        )
        return

    if conforms:
        return

    sh = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    for result in report_graph.subjects(predicate=RDF_TYPE, object=sh.ValidationResult):
        focus_node = next(report_graph.objects(result, sh.focusNode), None)
        result_message = next(report_graph.objects(result, sh.resultMessage), None)
        source_shape = next(report_graph.objects(result, sh.sourceShape), None)
        result_sev = next(report_graph.objects(result, sh.resultSeverity), None)

        # Scope SHACL output to URI-named subjects in the local matrix's
        # namespace. Otherwise every shape that targets a system-matrix class
        # would spam diagnostics across hundreds of terms the author can't
        # change, and the CLI's ResourceAnnotationShape would fire on every
        # blank node in the assembly (thousands per matrix). When
        # ``local_namespaces is None`` (older callers) we keep the legacy
        # behavior of emitting everything — used by unit tests that pre-date
        # this filter.
        if local_namespaces is not None:
            if not isinstance(focus_node, URIRef):
                continue  # blank-node subjects are noise here
            if not _focus_in_local_namespace(str(focus_node), local_namespaces):
                continue  # subject lives in a system or external namespace

        # The focus node moves to the structured `subject` field on the
        # diagnostic so the grouped renderer surfaces it as a section header
        # rather than embedding it in the prose. Keep `source_shape` in the
        # message — it's metadata, not a grouping key.
        parts = []
        if result_message:
            parts.append(str(result_message))
        if source_shape:
            parts.append(f"(shape <{source_shape}>)")
        message = " — ".join(parts) if parts else "SHACL violation"

        focus_subject = str(focus_node) if isinstance(focus_node, URIRef) else None
        focus_path = (
            subject_origin.get(focus_node)
            if subject_origin and isinstance(focus_node, URIRef)
            else None
        )
        _emit_shacl_finding(
            bag,
            str(result_sev) if result_sev else "",
            message,
            focus_subject,
            focus_path,
        )


# SHACL severity → our severity + code-band. The default severity in SHACL is
# sh:Violation (we treat as error). sh:Warning and sh:Info are the
# recommendation/hint tiers used by the IRI-node advisory shapes and the CLI's
# bundled ResourceAnnotationShape (see services/shapes/quality.ttl).
_SEVERITY_CODES = {
    str(SH_NS.Violation): ("PLGT_E0500", "SHACL violation"),
    str(SH_NS.Warning): ("PLGT_W0500", "SHACL warning"),
    str(SH_NS.Info): ("PLGT_I0500", "SHACL suggestion"),
}


def _emit_shacl_finding(
    bag: DiagnosticBag,
    severity_uri: str,
    message: str,
    subject: str | None,
    path: str | None,
) -> None:
    """Route a SHACL finding to the diagnostic bag by its ``sh:resultSeverity``.

    Unknown / absent severity defaults to ``sh:Violation`` (an error) so a
    misconfigured shape can never silently downgrade a finding.
    """
    code, label = _SEVERITY_CODES.get(severity_uri, ("PLGT_E0500", "SHACL violation"))
    emit = {
        "PLGT_E0500": bag.error,
        "PLGT_W0500": bag.warning,
        "PLGT_I0500": bag.info,
    }[code]
    emit(code, f"{label}: {message}", subject=subject, path=path)


def _focus_in_local_namespace(uri: str, local_namespaces: set[str]) -> bool:
    """True iff ``uri`` sits in any of the local matrix namespaces (which the
    pipeline already collects for namespace enforcement).
    """
    return any(uri.startswith(ns) for ns in local_namespaces)


def _merge_cli_quality_shapes(graph: Graph) -> None:
    """Merge CLI-bundled authoring-guidance shapes into the assembled graph.

    These shapes (currently the resource-annotation suggestions in
    ``services/shapes/quality.ttl``) are intentionally not published in the
    plgt system matrix — they're developer-facing hints that would be
    noise in platform-side validation. Parse failure or a missing resource
    is a packaging bug; surfacing it via the same diagnostic path as a
    pyshacl crash would obscure it, so we let the exception propagate and be
    caught by the surrounding ``_run_shacl_validation`` try/except.
    """
    shape_file = resources.files("plgt.services.shapes") / "quality.ttl"
    graph.parse(data=shape_file.read_text(), format="turtle")


def _extract_and_strip_iri_node_shapes(graph: Graph) -> list[_IRINodeConstraint]:
    """Find every NodeShape whose ``sh:target`` object is typed
    ``plgt:IRINodeTarget``, extract its ``sh:property`` constraints for native
    evaluation, and remove those shapes from ``graph`` so a standard SHACL
    engine never loads the opaque custom target.

    A NodeShape qualifies when one of its ``sh:target`` objects carries
    ``a plgt:IRINodeTarget``. For each such shape we read every ``sh:property``
    constraint (path, minCount, severity, message, source shape IRI) generically
    from the graph — nothing is hardcoded — then strip the NodeShape's triples,
    its ``sh:target`` blank-node closure, and each referenced property shape's
    closure. Mutates ``graph`` in place; returns the constraints (which may be
    empty when no such shape is present).
    """
    target_objs = {
        target
        for target in graph.objects(None, SH_NS.target)
        if (target, RDF_TYPE, PLGT_IRI_NODE_TARGET) in graph
    }
    if not target_objs:
        return []
    node_shapes = {
        subject
        for target in target_objs
        for subject in graph.subjects(SH_NS.target, target)
    }

    constraints: list[_IRINodeConstraint] = []
    for shape in node_shapes:
        for prop in graph.objects(shape, SH_NS.property):
            constraint = _read_iri_node_constraint(graph, prop)
            if constraint is not None:
                constraints.append(constraint)

    # Strip the qualifying shapes (and their property/target closures) so
    # pyshacl never sees the custom target.
    for shape in node_shapes:
        for prop in list(graph.objects(shape, SH_NS.property)):
            _remove_node_closure(graph, prop)
        for target in list(graph.objects(shape, SH_NS.target)):
            _remove_node_closure(graph, target)
        _remove_node_closure(graph, shape)

    return constraints


def _read_iri_node_constraint(graph: Graph, prop_shape) -> _IRINodeConstraint | None:
    """Read one ``sh:property`` of a ``plgt:IRINodeTarget`` shape into an
    ``_IRINodeConstraint``. Returns ``None`` when the property shape has no
    ``sh:path`` URI (nothing to evaluate). ``sh:minCount`` defaults to 1 when
    absent — the advisory shapes are minCount-only nudges.
    """
    path = next(graph.objects(prop_shape, SH_NS.path), None)
    if not isinstance(path, URIRef):
        return None

    min_count_lit = next(graph.objects(prop_shape, SH_NS.minCount), None)
    try:
        min_count = int(min_count_lit) if min_count_lit is not None else 1
    except (TypeError, ValueError):
        min_count = 1

    severity = next(graph.objects(prop_shape, SH_NS.severity), None)
    severity = severity if isinstance(severity, URIRef) else None

    message_lit = next(graph.objects(prop_shape, SH_NS.message), None)
    message = str(message_lit) if message_lit is not None else None

    source_shape = str(prop_shape) if isinstance(prop_shape, URIRef) else None

    return _IRINodeConstraint(path, min_count, severity, message, source_shape)


def _remove_node_closure(graph: Graph, root) -> None:
    """Remove ``root``'s triples and the closure of every blank node reachable
    from it. Named (URI) objects are NOT followed — only the anonymous
    structure that belongs to ``root`` — so stripping one shape never deletes a
    sibling shape that happens to be referenced by URI.
    """
    nested_blanks = [
        obj for obj in graph.objects(root, None) if isinstance(obj, rdflib.BNode)
    ]
    graph.remove((root, None, None))
    for nested in nested_blanks:
        _remove_node_closure(graph, nested)


def _evaluate_iri_node_targets(
    data_graph: Graph,
    constraints: list[_IRINodeConstraint],
    bag: DiagnosticBag,
    *,
    local_namespaces: set[str] | None = None,
    subject_origin: dict[URIRef, str] | None = None,
) -> None:
    """Native evaluation of the ``plgt:IRINodeTarget`` advisory shapes.

    For each named IRI subject in ``data_graph`` and each ``constraint``: count
    the node's values for the constraint's ``sh:path``; when below
    ``min_count`` emit a finding carrying the constraint's severity and message.
    Blank nodes are skipped (``isURI`` is the custom target's selector). The
    custom target selects nodes in subject OR object position; iterating the
    data graph's subjects covers exactly the package's own authored resources
    (the only ones that can be missing a label/comment the author controls).

    When ``local_namespaces`` is supplied, only nodes in those namespaces are
    reported (dependency resources are never the author's to fix). When
    ``None`` (legacy unit-test callers) every IRI subject is reported.
    """
    if not constraints:
        return
    seen: set[tuple[str, str]] = set()
    for node in set(data_graph.subjects()):
        if not isinstance(node, URIRef):
            continue  # blank nodes are never targeted
        node_str = str(node)
        if local_namespaces is not None and not _focus_in_local_namespace(
            node_str, local_namespaces
        ):
            continue
        for c in constraints:
            if len(list(data_graph.objects(node, c.path))) >= c.min_count:
                continue
            key = (node_str, str(c.path))
            if key in seen:
                continue  # overlapping imports can register identical constraints
            seen.add(key)
            parts = []
            if c.message:
                parts.append(c.message)
            if c.source_shape:
                parts.append(f"(shape <{c.source_shape}>)")
            message = " — ".join(parts) if parts else "SHACL violation"
            path = subject_origin.get(node) if subject_origin else None
            _emit_shacl_finding(
                bag,
                str(c.severity) if c.severity else "",
                message,
                node_str,
                path,
            )


# ---------------------------------------------------------------------------
# Phase 7: predicate / class existence + did-you-mean

# URIs we never check for "declared in assembled graph". These vocabularies are external
# (rdf/rdfs/owl/xsd) or so foundational (shacl, dct, dprod) that requiring them to appear
# explicitly in the cache would generate noise on every project. The list intentionally
# overlaps with _STANDARD_VOCABULARIES used by import-vs-deps; keeping them separate so
# the predicate check can be tuned independently.
_UNCHECKED_PREDICATE_NAMESPACES = frozenset(
    {
        "http://www.w3.org/2000/01/rdf-schema#",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "http://www.w3.org/2002/07/owl#",
        "http://www.w3.org/2004/02/skos/core#",
        "http://www.w3.org/ns/shacl#",
        "http://www.w3.org/2001/XMLSchema#",
        "http://purl.org/dc/terms/",
        "http://purl.org/dc/elements/1.1/",
        "https://ekgf.github.io/dprod/",
        "https://w3id.org/function/ontology#",  # fno
        # Common community vocabularies authors may reference without caching as deps.
        "http://xmlns.com/foaf/0.1/",  # foaf
        "http://www.w3.org/ns/prov#",  # prov
        "http://purl.org/vocab/vann/",  # vann
        "http://purl.org/vocommons/voaf#",  # voaf
        "http://creativecommons.org/ns#",  # cc
        "http://www.w3.org/2006/time#",  # owl-time
        "http://www.w3.org/2003/01/geo/wgs84_pos#",  # geo
        "http://schema.org/",  # schema.org (http canonical)
        "https://schema.org/",  # schema.org (https variant in the wild)
        # rml namespaces — separate concern from plgt, not validated here
        "http://semweb.mmlab.be/ns/rml#",  # legacy RML
        "http://w3id.org/rml/",  # RML 2.0 core
        "http://www.w3.org/ns/r2rml#",
        "http://www.w3.org/ns/dcat#",  # DCAT data catalog vocab
    }
)


def _split_uri(uri: str) -> tuple[str, str]:
    """Split a URI into ``(namespace, local_name)``. The namespace is the
    portion up to and including the last ``#`` or ``/``. Falls back to
    ``(uri, "")`` if no separator exists.
    """
    for sep in ("#", "/"):
        idx = uri.rfind(sep)
        if idx >= 0:
            return uri[: idx + 1], uri[idx + 1 :]
    return uri, ""


def _build_term_index(graph: Graph) -> dict[str, list[str]]:
    """Build a namespace → list-of-local-names index from every URI that
    appears as a subject in ``graph``. Used by did-you-mean suggestions.

    The heuristic that "a term is declared if it's a subject somewhere"
    is broad but precise enough in practice: every term that an ontology
    introduces appears as a subject of at least one triple
    (``rdfs:label``, ``rdf:type``, etc.).
    """
    index: dict[str, list[str]] = {}
    for subject in graph.subjects():
        if not isinstance(subject, URIRef):
            continue
        ns, local = _split_uri(str(subject))
        if not local:
            continue
        index.setdefault(ns, []).append(local)
    # Dedup while preserving stable order so test output is reproducible.
    for ns, names in index.items():
        index[ns] = sorted(set(names))
    return index


def _check_predicate_class_existence(
    *,
    local_graph: Graph,
    assembled: Graph,
    bag: DiagnosticBag,
    file_origin: dict[tuple, str] | None = None,
    script_origin: dict[tuple, str] | None = None,
    project_dir: Path | None = None,
) -> None:
    """Every predicate URI and ``rdf:type`` class URI in the local graph
    must resolve to a declared term in the assembled graph. Emits
    ``PLGT_E0201`` per unknown predicate, ``PLGT_E0202`` per unknown class.

    "Declared" = appears as a subject of any triple in the assembled
    graph. Standard external vocabularies (rdf, rdfs, owl, xsd, etc.) are
    exempt: we never have them in the cache and the server doesn't
    require them there.

    Did-you-mean uses ``difflib.get_close_matches`` over the index of
    terms in the same namespace. A single suggestion is attached to the
    diagnostic (the closest match above the default cutoff).

    When ``file_origin`` is supplied, each diagnostic carries the TTL
    file that authored the offending triple plus a best-effort line
    number (located by scanning the file for the prefixed-name form or
    the full URI). LSP clients use those to pin squiggles to the source.
    """
    import difflib

    declared_uris: set[str] = set()
    for s in assembled.subjects():
        if isinstance(s, URIRef):
            declared_uris.add(str(s))

    term_index = _build_term_index(assembled)
    namespace_prefixes = {str(ns): prefix for prefix, ns in local_graph.namespaces()}
    _line_cache: dict[str, list[str]] = {}

    def _file_lines(path: str) -> list[str]:
        if path not in _line_cache:
            try:
                full = (project_dir / path) if project_dir else Path(path)
                _line_cache[path] = full.read_text(encoding="utf-8").splitlines()
            except OSError:
                _line_cache[path] = []
        return _line_cache[path]

    def _find_line(path: str | None, uri: str) -> int | None:
        if not path:
            return None
        ns, local = _split_uri(uri)
        prefix = namespace_prefixes.get(ns)
        needles: list[str] = [f"<{uri}>"]
        if prefix is not None:
            # `prefix == ""` is the empty/default prefix — search for `:local`.
            needles.append(f"{prefix}:{local}")
        for i, line in enumerate(_file_lines(path), start=1):
            if any(n in line for n in needles):
                return i
        return None

    def _suggest(unknown_uri: str) -> str | None:
        ns, local = _split_uri(unknown_uri)
        candidates = term_index.get(ns) or []
        if not candidates:
            return None
        match = difflib.get_close_matches(local, candidates, n=1, cutoff=0.7)
        return f"{ns}{match[0]}" if match else None

    def _should_check(uri: str) -> bool:
        ns, _ = _split_uri(uri)
        return ns not in _UNCHECKED_PREDICATE_NAMESPACES

    reported_predicates: set[str] = set()
    reported_classes: set[str] = set()

    for s, predicate, obj in local_graph:
        triple_subject = str(s) if isinstance(s, URIRef) else None
        if isinstance(predicate, URIRef):
            puri = str(predicate)
            if (
                _should_check(puri)
                and puri not in declared_uris
                and puri not in reported_predicates
            ):
                reported_predicates.add(puri)
                path = file_origin.get((s, predicate, obj)) if file_origin else None
                bag.error(
                    "PLGT_E0201",
                    f"Unknown predicate: <{puri}>",
                    suggest=_suggest(puri),
                    path=path,
                    line=_find_line(path, puri),
                    subject=triple_subject,
                )
        if predicate == RDF_TYPE and isinstance(obj, URIRef):
            curi = str(obj)
            if (
                _should_check(curi)
                and curi not in declared_uris
                and curi not in reported_classes
            ):
                reported_classes.add(curi)
                path = file_origin.get((s, predicate, obj)) if file_origin else None
                bag.error(
                    "PLGT_E0202",
                    f"Unknown class: <{curi}>",
                    suggest=_suggest(curi),
                    path=path,
                    line=_find_line(path, curi),
                    subject=triple_subject,
                )

    # Also walk SPARQL bodies (inlined after script:// expansion as string literals on
    # SPARQL-bearing predicates). Prefixed names there resolve against the local graph's
    # namespace table; an unresolvable prefix is left alone (the SPARQL parser already
    # flagged it). Names whose resolved URI is unknown surface as PLGT_E0203, distinct from
    # E0201 so authors can tell "missing predicate in TTL" from "missing in SPARQL body".
    # SPARQL bodies live either in their own .rq file (script:// case — script_origin
    # maps (s, p) to that file) or inline as a triple-quoted literal in the TTL that
    # carries them (file_origin's subject map gives that TTL). We prefer the .rq
    # path when available so the LSP squiggle lands on the actual body, not the
    # TTL that references it.
    prefix_table = {prefix: str(ns) for prefix, ns in local_graph.namespaces()}
    subject_origin: dict[URIRef, str] = {}
    if file_origin:
        for (s_key, _p, _o), p_path in file_origin.items():
            if isinstance(s_key, URIRef):
                subject_origin.setdefault(s_key, p_path)
    reported_in_sparql: set[str] = set()
    for s, predicate, obj in local_graph.triples((None, None, None)):
        if predicate not in SPARQL_BEARING_PREDICATES:
            continue
        if not isinstance(obj, Literal):
            # script:// refs are URIRef before expansion; after expansion they become
            # Literal. We only check post-expansion strings.
            continue
        # Prefer the .rq path (when this body came from a script://), otherwise
        # fall back to the TTL that declares the subject.
        body_path: str | None = None
        if script_origin and (s, predicate) in script_origin:
            body_path = script_origin[(s, predicate)]
        elif isinstance(s, URIRef):
            body_path = subject_origin.get(s)
        body_text = str(obj)
        for prefix, local in extract_prefixed_names(body_text):
            namespace_uri = prefix_table.get(prefix)
            if namespace_uri is None:
                # Unknown prefix: SPARQL parse phase already errored, skip.
                continue
            full = f"{namespace_uri}{local}"
            if not _should_check(full):
                continue
            if full in declared_uris or full in reported_in_sparql:
                continue
            reported_in_sparql.add(full)
            line = (
                _find_line(body_path, full)
                if (script_origin and (s, predicate) in script_origin)
                else None
            )
            bag.error(
                "PLGT_E0203",
                f"Unknown term in SPARQL body: <{full}>",
                suggest=_suggest(full),
                path=body_path,
                line=line,
                subject=str(s) if isinstance(s, URIRef) else None,
            )


# ---------------------------------------------------------------------------
# Phase 8: GREL function call validation


def _check_grel_function_calls(
    *,
    local_graph: Graph,
    assembled: Graph,
    bag: DiagnosticBag,
    file_origin: dict[tuple, str] | None = None,
    project_dir: Path | None = None,
) -> None:
    """Every ``plgt:executesFunction`` target in the local graph must
    resolve to a declared function (URI present as a subject in the
    assembled graph). When ``fno:expects`` is declared on the function,
    surface its expected-parameter count alongside any ``fno:expects``
    parameter list on the call site (currently informational; argument
    arity is checked when the local site supplies an
    explicit parameter list).

    Diagnostics:

    * ``PLGT_E0601`` — unknown function URI in ``plgt:executesFunction``.
    * ``PLGT_E0602`` — call site declares its own ``fno:expects`` whose
      length does not match the function's declared parameter list.
    """
    declared_subjects: set[str] = set()
    for s in assembled.subjects():
        if isinstance(s, URIRef):
            declared_subjects.add(str(s))

    # Build a map function URI → declared parameter-list length (or None when unknown).
    function_arity: dict[str, int] = {}
    for fn, _p, plist in assembled.triples((None, FNO_EXPECTS, None)):
        if not isinstance(fn, URIRef):
            continue
        try:
            arity = _bounded_collection_len(assembled, plist)
        except Exception:  # noqa: BLE001 — surface as "unknown" + debug log; don't crash phase
            logger.debug(
                "Failed to compute fno:expects length for %s", fn, exc_info=True
            )
            arity = -1
        if arity >= 0:
            function_arity[str(fn)] = arity

    namespace_prefixes = {str(ns): prefix for prefix, ns in local_graph.namespaces()}
    _line_cache: dict[str, list[str]] = {}

    def _file_lines(p: str) -> list[str]:
        if p not in _line_cache:
            try:
                full = (project_dir / p) if project_dir else Path(p)
                _line_cache[p] = full.read_text(encoding="utf-8").splitlines()
            except OSError:
                _line_cache[p] = []
        return _line_cache[p]

    def _find_line(path: str | None, uri: str) -> int | None:
        if not path:
            return None
        ns, local = _split_uri(uri)
        prefix = namespace_prefixes.get(ns)
        needles: list[str] = [f"<{uri}>"]
        if prefix is not None:
            needles.append(f"{prefix}:{local}")
        for i, line in enumerate(_file_lines(path), start=1):
            if any(n in line for n in needles):
                return i
        return None

    reported_functions: set[str] = set()
    for caller, _p, fn in local_graph.triples((None, PLGT_OS_EXECUTES_FUNCTION, None)):
        if not isinstance(fn, URIRef):
            continue
        fn_uri = str(fn)
        caller_subject = str(caller) if isinstance(caller, URIRef) else None
        if fn_uri in reported_functions:
            continue
        if fn_uri not in declared_subjects:
            reported_functions.add(fn_uri)
            call_path = (
                file_origin.get((caller, PLGT_OS_EXECUTES_FUNCTION, fn))
                if file_origin
                else None
            )
            bag.error(
                "PLGT_E0601",
                f"Unknown function in plgt:executesFunction: <{fn_uri}>",
                path=call_path,
                line=_find_line(call_path, fn_uri),
                subject=caller_subject,
            )
            continue
        # Call-site arity check: when the local graph declares fno:expects on the caller,
        # its length should match the function's declared arity (when known).
        local_expects = list(local_graph.triples((caller, FNO_EXPECTS, None)))
        if local_expects and fn_uri in function_arity:
            try:
                call_arity = _bounded_collection_len(local_graph, local_expects[0][2])
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Failed to compute call-site fno:expects length for %s -> %s",
                    caller,
                    fn_uri,
                    exc_info=True,
                )
                continue
            expected = function_arity[fn_uri]
            if call_arity != expected:
                bag.error(
                    "PLGT_E0602",
                    f"Function <{fn_uri}> expects {expected} arguments; "
                    f"call supplies {call_arity}.",
                    subject=caller_subject,
                )


_MAX_COLLECTION_LEN = 1024


def _bounded_collection_len(graph: Graph, head) -> int:
    """``len(Collection(...))`` walks the ``rdf:rest`` chain to ``rdf:nil``.
    A malformed graph with a cycle (``l1 rdf:rest l2; l2 rdf:rest l1``)
    would loop forever. Bound the walk so a corrupt list cannot wedge the
    validator; an over-long list raises and the caller treats arity as
    unknown.
    """
    coll = Collection(graph, head)
    count = 0
    for _ in coll:
        count += 1
        if count > _MAX_COLLECTION_LEN:
            msg = (
                f"RDF list exceeds bounded length {_MAX_COLLECTION_LEN} "
                "(possible cycle)"
            )
            raise ValueError(msg)
    return count


# ---------------------------------------------------------------------------
# Phase 9: variable / secret resolution sanity


def _check_variables_and_secrets(
    *,
    local_graph: Graph,
    bag: DiagnosticBag,
    subject_origin: dict[URIRef, str] | None = None,
) -> None:
    """Validate ``plgt-build:Variable`` and ``plgt-scrt:ManagedSecret``
    declarations.

    Required fields per the system matrix shape:

    * Variable: ``plgt-build:variableType`` (URI), ``plgt-build:label``
      (literal), ``plgt-build:required`` (boolean).
    * Secret: ``plgt-scrt:description`` (literal) — the only field
      required directly on a ``ManagedSecret`` instance. ``rdfs:isDefinedBy``
      and ``plgt-iam:hasPolicy`` are also required by SHACL but are checked
      by phase 6 (PLGT_E0500); we don't duplicate them here.

    Unbound variable bindings at activation time are deferred to the
    platform; this phase emits a single ``PLGT_W0701`` informational
    note when any variables are declared, to remind the author that
    their resolution is deferred.

    Diagnostics:

    * ``PLGT_E0701`` — required field missing on a variable declaration.
    * ``PLGT_E0702`` — required field missing on a secret declaration.
    * ``PLGT_W0701`` — informational reminder that variable bindings are
      deferred to activation time.

    Note: duplicate-URI detection across variables / secrets was scoped
    out of this phase because ``rdflib.Graph`` is set-semantics — two TTL
    files declaring the same URI as a ``Variable`` merge into a single
    triple at parse time, so the merged graph can never expose two
    distinct subjects with the same URI. Per-file duplicate detection
    needs the same per-file source tracking the namespace-enforcement
    phase wants; it lands together with that work, not here.
    """
    origin = subject_origin or {}

    variable_count = 0
    seen_variables: set[str] = set()
    for subj in local_graph.subjects(RDF_TYPE, PLGT_BUILD_VARIABLE):
        if not isinstance(subj, URIRef):
            continue
        uri = str(subj)
        if uri in seen_variables:
            # Cannot reach here in practice (set semantics), but the guard makes the
            # intent explicit and keeps the count honest if rdflib's behavior changes.
            continue
        seen_variables.add(uri)
        variable_count += 1
        if not any(local_graph.objects(subj, PLGT_BUILD_VARIABLE_TYPE)):
            bag.error(
                "PLGT_E0701",
                f"Variable <{uri}> is missing required field plgt-build:variableType",
            )
        if not any(local_graph.objects(subj, PLGT_BUILD_LABEL)):
            bag.error(
                "PLGT_E0701",
                f"Variable <{uri}> is missing required field plgt-build:label",
            )
        if not any(local_graph.objects(subj, PLGT_BUILD_REQUIRED)):
            bag.error(
                "PLGT_E0701",
                f"Variable <{uri}> is missing required field plgt-build:required",
            )

    seen_secrets: set[str] = set()
    for subj in local_graph.subjects(RDF_TYPE, PLGT_SCRT_MANAGED_SECRET):
        if not isinstance(subj, URIRef):
            continue
        uri = str(subj)
        if uri in seen_secrets:
            continue
        seen_secrets.add(uri)
        path = origin.get(subj)
        if not any(local_graph.objects(subj, PLGT_SCRT_DESCRIPTION)):
            bag.error(
                "PLGT_E0702",
                f"Secret <{uri}> is missing required field plgt-scrt:description",
                path=path,
                subject=uri,
            )

    if variable_count:
        bag.warning(
            "PLGT_W0701",
            f"Project declares {variable_count} variable(s). Their values are "
            "deferred to activation time; the platform validates bindings on "
            "install.",
        )


# ---------------------------------------------------------------------------


def collect_imports_from_local_graph(graph: Graph) -> Iterable[URIRef]:
    """Helper used by tests / downstream commands: every URI imported by a
    matrix in the local graph. Iteration is stable in URI lexicographic
    order.
    """
    return sorted(
        {
            obj
            for _, _, obj in graph.triples((None, PLGT_MTX_IMPORTS, None))
            if isinstance(obj, URIRef)
        },
        key=str,
    )


__all__ = [
    "ValidationResult",
    "collect_imports_from_local_graph",
    "validate_project",
]
