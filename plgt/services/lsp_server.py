"""Language Server Protocol implementation for matrix authoring.

Wraps the validation pipeline + schema service as an LSP server so editors
get in-place diagnostics, hover, go-to-definition, and completion without
shelling out to ``plgt validate`` each save.

Architecture:

* Single workspace per server instance. Workspace root = ``rootUri`` from
  the LSP ``initialize`` request.
* On open / save: schedule a debounced re-run of ``validate_project`` and
  push ``textDocument/publishDiagnostics`` to the client. Successive saves
  within the debounce window cancel the prior task, so rapid typing does
  not queue serial full revalidations.
* Hover / definition / completion: serve from a cached assembled graph
  invalidated by the same save/open events. First call (cold cache) pays
  for ``validate_project``; subsequent calls reuse the graph and the
  term-index built alongside it.

The server runs on stdio (``pygls``'s default). The ``plgt lsp`` command
launches it; editors point at ``plgt lsp`` as the server binary.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from plgt.services.deps_install_service import lockfile_path_for
from plgt.services.diagnostics import Diagnostic, Severity
from plgt.services.formatter import format_sparql, format_turtle
from plgt.services.schema_service import describe_term, list_terms
from plgt.services.validation_pipeline import ValidationResult, validate_project

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rdflib import Graph

logger = logging.getLogger(__name__)


SERVER_NAME = "plgt-lsp"
SERVER_VERSION = "0.1.0"

# Debounce window for re-running validation. Save handlers schedule a task that sleeps for
# this long before invoking validate_project; a superseding save cancels the prior task so
# only the last edit's validation actually runs.
_DEBOUNCE_SECONDS = 0.3


@dataclass
class _ServerState:
    """Per-server state for cache + debounce. Attached to the ``LanguageServer``
    instance as ``_plgt_state`` so handlers can reach it without globals.

    A single editor workspace can contain multiple matrix projects, each with
    its own ``poliglot.yml``. Caches are keyed by project root so opening
    files from different projects doesn't trample each other's state.
    """

    # Validation result + published-diagnostics URIs + missing-lockfile-warning
    # flag, all keyed by the project root the data is for. Keys are absolute
    # ``Path`` objects pointing at the directory that contains ``poliglot.yml``.
    cached_results: dict[Path, ValidationResult] = field(default_factory=dict)
    published_diag_uris_by_project: dict[Path, set[str]] = field(default_factory=dict)
    warned_missing_lockfile: set[Path] = field(default_factory=set)
    # Lookup: which project does this document URI belong to? Populated on
    # didOpen/didSave so hover/definition/completion handlers can resolve the
    # right project's assembled graph from a ``textDocument.uri`` alone.
    project_for_uri: dict[str, Path] = field(default_factory=dict)
    pending_diagnostics_task: asyncio.Task | None = None
    # URI whose project is queued for refresh by the debounced task. The
    # debounce coalesces rapid saves so only the latest file's project is
    # actually re-validated.
    pending_refresh_uri: str | None = None
    # Cached workspace-mode resolution (slug for workspace-sync, None for registry-resolve).
    # Read once on initialize and reused for every validation; users restarting the editor
    # picks up a new default-workspace setting from plgt config. A future improvement is to
    # accept an LSP `initializationOptions.workspace` override so editors can target a
    # specific workspace without a config-file edit.
    workspace_mode: str | None = None
    workspace_mode_resolved: bool = False


def _state(server: LanguageServer) -> _ServerState:
    state = getattr(server, "_plgt_state", None)
    if state is None:
        state = _ServerState()
        server._plgt_state = state  # noqa: SLF001 — attaching session state
    return state


def _resolve_workspace_for_server(
    server: LanguageServer, project_root: Path | None = None
) -> str | None:
    """Return the workspace slug to use for validation, or None for
    registry-resolve mode.

    Resolution order:

    1. The configured default workspace (``defaults.workspace`` in the plgt
       config). Cached on the server's state so the plgt config isn't
       re-parsed on every keystroke.
    2. When ``project_root`` is supplied, *verify the lockfile exists for
       that project*. If the configured workspace's lockfile is missing
       but ``_registry.lock`` is present, fall back to registry mode so
       the editor still gets diagnostics / hover / completion. (Without
       this fallback the user gets PLGT_E0900 and no graph until they
       run ``plgt sync --from-workspace <slug>`` manually.)
    """
    state = _state(server)
    if not state.workspace_mode_resolved:
        try:
            from plgt.utils.workspace_mode import resolve_workspace_mode

            state.workspace_mode = resolve_workspace_mode(
                from_workspace=None, from_registry=False
            )
        except Exception:
            logger.exception(
                "Failed to resolve default workspace; falling back to registry mode"
            )
            state.workspace_mode = None
        state.workspace_mode_resolved = True

    configured = state.workspace_mode
    if project_root is None or configured is None:
        return configured

    # Per-project fallback: if the configured workspace's lockfile isn't
    # populated for this project but _registry.lock is, use registry mode.
    workspace_lock = lockfile_path_for(project_root, configured)
    registry_lock = lockfile_path_for(project_root, None)
    if not workspace_lock.exists() and registry_lock.exists():
        return None
    return configured


def create_server() -> LanguageServer:
    """Build a configured LanguageServer instance. Separated so unit tests
    can instantiate the server without launching stdio.
    """
    server = LanguageServer(name=SERVER_NAME, version=SERVER_VERSION)
    _register_handlers(server)
    return server


def run_stdio() -> None:
    """Launch the server on stdio. The CLI ``plgt lsp`` entry point calls
    this; it blocks until the editor disconnects.

    Stdout is reserved for the LSP wire protocol — every log byte that
    leaks there will desync the editor's parser. ``logging.basicConfig``
    is force-targeted at stderr (``force=True`` to override any handler
    typer/rich set up earlier in this process), and pygls' own loggers
    are reattached to that handler.
    """
    import sys

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        force=True,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # pygls registers handlers eagerly on import; rebind so they obey the
    # stderr-only rule above.
    for name in ("pygls", "pygls.server", "pygls.protocol"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    create_server().start_io()


# ---------------------------------------------------------------------------


def _register_handlers(server: LanguageServer) -> None:
    @server.feature(lsp.INITIALIZED)
    def on_initialized(ls: LanguageServer, params: lsp.InitializedParams) -> None:
        # Run an initial validation pass over the workspace so the editor
        # gets diagnostics before any file is touched. The initial pass is
        # synchronous because there is no prior task to debounce against.
        _refresh_validation(ls)

    @server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
    def on_save(
        ls: LanguageServer,
        params: lsp.DidSaveTextDocumentParams,
    ) -> None:
        _schedule_debounced_refresh(ls, params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
    def on_open(
        ls: LanguageServer,
        params: lsp.DidOpenTextDocumentParams,
    ) -> None:
        _schedule_debounced_refresh(ls, params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_HOVER)
    def on_hover(
        ls: LanguageServer,
        params: lsp.HoverParams,
    ) -> lsp.Hover | None:
        graph = _assembled_graph_for(ls, params.text_document.uri)
        if graph is None:
            return None
        text = _read_document(ls, params.text_document.uri)
        if text is None:
            return None
        prefix_table = _prefix_table_for_document(ls, params.text_document.uri, graph)
        uri = _extract_prefixed_name_at(text, params.position, prefix_table)
        if uri is None:
            return None
        description = describe_term(graph, uri)
        if description is None:
            return None
        return lsp.Hover(
            contents=lsp.MarkupContent(
                kind=lsp.MarkupKind.Markdown,
                value=_format_hover(description),
            )
        )

    @server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
    def on_definition(
        ls: LanguageServer,
        params: lsp.DefinitionParams,
    ) -> list[lsp.Location] | None:
        graph = _assembled_graph_for(ls, params.text_document.uri)
        if graph is None:
            return None
        text = _read_document(ls, params.text_document.uri)
        if text is None:
            return None
        prefix_table = _prefix_table_for_document(ls, params.text_document.uri, graph)
        uri = _extract_prefixed_name_at(text, params.position, prefix_table)
        if uri is None:
            return None
        location = _locate_definition_site(ls, uri, graph)
        return [location] if location else None

    @server.feature(
        lsp.TEXT_DOCUMENT_COMPLETION,
        lsp.CompletionOptions(trigger_characters=[":"]),
    )
    def on_completion(
        ls: LanguageServer,
        params: lsp.CompletionParams,
    ) -> lsp.CompletionList | None:
        graph = _assembled_graph_for(ls, params.text_document.uri)
        if graph is None:
            return None
        text = _read_document(ls, params.text_document.uri)
        if text is None:
            return None
        prefix = _prefix_label_before_cursor(text, params.position)
        if prefix is None:
            return None
        prefix_table = _prefix_table_for_document(ls, params.text_document.uri, graph)
        namespace = prefix_table.get(prefix)
        if namespace is None:
            return None
        items: list[lsp.CompletionItem] = []
        for term in list_terms(graph):
            if not term["uri"].startswith(str(namespace)):
                continue
            local_name = term["uri"][len(str(namespace)) :]
            if not local_name:
                continue
            detail = ", ".join(term.get("types") or [])
            label = term.get("label") or local_name
            items.append(
                lsp.CompletionItem(
                    label=local_name,
                    kind=lsp.CompletionItemKind.Class,
                    detail=detail or None,
                    documentation=label if label != local_name else None,
                )
            )
        return lsp.CompletionList(is_incomplete=False, items=items)

    @server.feature(lsp.TEXT_DOCUMENT_FORMATTING)
    def on_formatting(
        ls: LanguageServer,
        params: lsp.DocumentFormattingParams,
    ) -> list[lsp.TextEdit] | None:
        """Reformat the whole document. Dispatches by language id —
        falls back to the file extension when the editor didn't send a
        language id (e.g. for `.rq` files that VS Code doesn't recognise
        out of the box, our grammar package supplies the id)."""
        return _format_document(ls, params.text_document.uri)

    @server.feature(lsp.TEXT_DOCUMENT_RANGE_FORMATTING)
    def on_range_formatting(
        ls: LanguageServer,
        params: lsp.DocumentRangeFormattingParams,
    ) -> list[lsp.TextEdit] | None:
        """Range formatting: the LSP spec says we should format only the
        requested range. We format the *whole document* and let the client
        merge — this is what `prettier`, `black`, and `ruff format` do in
        their LSP integrations because a token-stream formatter can't
        meaningfully restrict to an arbitrary range without losing
        structural context. Editors are happy with this in practice."""
        return _format_document(ls, params.text_document.uri)


# ---------------------------------------------------------------------------


def _find_project_root(file_path: Path, workspace_root: Path) -> Path | None:
    """Walk up from ``file_path`` looking for the nearest ``poliglot.yml``,
    capped at ``workspace_root``. Returns the directory containing the
    poliglot.yml, or ``None`` when the file is outside the workspace or in
    no matrix project at all.
    """
    try:
        file_path = file_path.resolve()
        workspace_root = workspace_root.resolve()
    except OSError:
        return None
    try:
        file_path.relative_to(workspace_root)
    except ValueError:
        return None  # file outside the workspace; ignore
    current = file_path if file_path.is_dir() else file_path.parent
    while True:
        if (current / "poliglot.yml").is_file():
            return current
        if current == workspace_root:
            return None
        if current.parent == current:
            return None  # filesystem root reached
        current = current.parent


def _path_from_uri(uri: str) -> Path | None:
    """Best-effort ``file://`` URI → Path. Returns None for non-file URIs."""
    if not uri.startswith("file://"):
        return None
    try:
        parsed = urlparse(uri)
        return Path(unquote(parsed.path))
    except Exception:  # noqa: BLE001 — never break the LSP loop on a malformed URI
        return None


def _refresh_project(server: LanguageServer, project_root: Path) -> None:
    """Validate ``project_root`` (a directory containing ``poliglot.yml``),
    cache the result, and publish diagnostics scoped to files within it.

    Per-project state means opening files from two different projects in the
    same VS Code window doesn't make them clobber each other's diagnostics
    or warnings.
    """
    state = _state(server)
    workspace = _resolve_workspace_for_server(server, project_root)
    result = validate_project(project_root, workspace=workspace)
    state.cached_results[project_root] = result

    # Surface a missing-lockfile precondition as a user-visible message exactly once
    # per project per session. The diagnostic is in the bag too; the popup just
    # makes the gap obvious before the user starts hunting for missing terms in an
    # empty graph.
    if result.assembled is None and project_root not in state.warned_missing_lockfile:
        lockfile_path = lockfile_path_for(project_root, workspace=workspace)
        if not lockfile_path.exists():
            state.warned_missing_lockfile.add(project_root)
            rel_lockfile = lockfile_path.relative_to(project_root)
            hint = (
                f"plgt sync --from-workspace {workspace}"
                if workspace
                else "plgt sync --from-registry"
            )
            try:
                server.window_show_message(
                    lsp.ShowMessageParams(
                        type=lsp.MessageType.Warning,
                        message=(
                            f"Poliglot ({project_root.name}): no {rel_lockfile} "
                            f"found. Run `{hint}` from {project_root.name}/ to "
                            "populate the dependency cache; validation and "
                            "hover/completion will be limited until then."
                        ),
                    )
                )
            except Exception:  # noqa: BLE001 — pygls v2 method name may vary
                logger.debug("Could not show missing-lockfile popup", exc_info=True)

    by_file: dict[str, list[lsp.Diagnostic]] = {}
    for d in result.diagnostics.sorted():
        path = d.path or ""
        full_path = project_root / path if path else project_root
        uri = full_path.as_uri()
        by_file.setdefault(uri, []).append(_to_lsp_diagnostic(d, project_root))

    # Clear stale squiggles from the previous refresh OF THIS PROJECT only — we
    # must never clear diagnostics for files in another project that had their
    # own refresh.
    previous = state.published_diag_uris_by_project.get(project_root, set())
    current: set[str] = set(by_file.keys())
    for uri in previous - current:
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )
    for uri, diagnostics in by_file.items():
        server.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
        )
    state.published_diag_uris_by_project[project_root] = current


def _refresh_validation(server: LanguageServer) -> None:
    """Workspace-wide refresh.

    If the workspace root itself contains a ``poliglot.yml`` (the
    single-project case — author rooted VS Code at the matrix project),
    refresh that. Otherwise it's a no-op: per-project discovery happens
    lazily via ``_refresh_validation_for_uri`` as files are opened.

    Kept as the single-argument legacy entrypoint so existing tests and
    handler call sites don't need to know about project roots.
    """
    workspace_root = _workspace_root(server)
    if workspace_root is None:
        return
    if (workspace_root / "poliglot.yml").is_file():
        _refresh_project(server, workspace_root)


def _refresh_validation_for_uri(server: LanguageServer, uri: str) -> None:
    """Find the matrix project that owns ``uri`` and refresh it. No-op when
    the URI is outside the workspace or any matrix project (e.g. the user
    opened a markdown file at the repo root).
    """
    workspace_root = _workspace_root(server)
    if workspace_root is None:
        return
    file_path = _path_from_uri(uri)
    if file_path is None:
        return
    project_root = _find_project_root(file_path, workspace_root)
    if project_root is None:
        return
    state = _state(server)
    state.project_for_uri[uri] = project_root
    _refresh_project(server, project_root)


def _schedule_debounced_refresh(server: LanguageServer, uri: str | None = None) -> None:
    """Cancel any in-flight validation task and schedule a fresh one after
    the debounce window. Saves arriving inside the window only keep the
    most recent task alive.

    When ``uri`` is supplied, the refresh targets just that file's matrix
    project. Without a URI the refresh falls back to the legacy
    workspace-root behavior, which handles the single-project case.
    """
    state = _state(server)
    if uri is not None:
        state.pending_refresh_uri = uri
    if (
        state.pending_diagnostics_task is not None
        and not state.pending_diagnostics_task.done()
    ):
        state.pending_diagnostics_task.cancel()

    async def _run() -> None:
        try:
            await asyncio.sleep(_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            queued = state.pending_refresh_uri
            state.pending_refresh_uri = None
            if queued is not None:
                _refresh_validation_for_uri(server, queued)
            else:
                _refresh_validation(server)
        except Exception:
            logger.exception("Debounced validation refresh failed")

    try:
        # `get_running_loop()` raises RuntimeError cleanly when no loop is running, never
        # implicitly creates one. Prefer it over `get_event_loop()` whose
        # implicit-create-on-no-loop behavior is deprecated and would silently turn
        # production into the no-loop fallback if Python ever removes the affordance.
        loop = asyncio.get_running_loop()
        state.pending_diagnostics_task = loop.create_task(_run())
    except RuntimeError:
        # No running loop (typical in unit tests that drive the server synchronously). Fall
        # back to a direct invocation so tests still exercise the refresh path.
        _refresh_validation(server)


def _to_lsp_diagnostic(d: Diagnostic, workspace_root: Path) -> lsp.Diagnostic:
    severity_map = {
        Severity.ERROR: lsp.DiagnosticSeverity.Error,
        Severity.WARNING: lsp.DiagnosticSeverity.Warning,
        Severity.INFO: lsp.DiagnosticSeverity.Information,
    }
    line = (d.line or 1) - 1
    col = (d.col or 1) - 1
    end_col = max(col, 0) + _token_length_at(workspace_root, d.path, line, col)
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=max(line, 0), character=max(col, 0)),
            end=lsp.Position(line=max(line, 0), character=end_col),
        ),
        severity=severity_map[d.severity],
        code=d.code,
        source=SERVER_NAME,
        message=d.message + (f"\n\nDid you mean: {d.suggest}" if d.suggest else ""),
    )


def _token_length_at(
    workspace_root: Path, path: str | None, line_idx: int, col_idx: int
) -> int:
    """Return the length of the token starting at ``line_idx:col_idx`` in
    ``path``, or 1 as a safe fallback. Reads the file lazily; cache misses
    here are cheap because we only invoke during diagnostic emission and
    only when a path/line/col is known.
    """
    if not path or line_idx < 0 or col_idx < 0:
        return 1
    full = workspace_root / path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        return 1
    lines = text.splitlines()
    if line_idx >= len(lines):
        return 1
    line = lines[line_idx]
    if col_idx >= len(line):
        return 1
    end = col_idx
    while end < len(line) and line[end] in _NAME_CHARS:
        end += 1
    return max(end - col_idx, 1)


def _workspace_root(server: LanguageServer) -> Path | None:
    workspace = server.workspace
    if workspace is None or workspace.root_path is None:
        return None
    return Path(workspace.root_path)


def _assembled_graph_for(
    server: LanguageServer, uri: str | None = None
) -> Graph | None:
    """Return the cached assembled graph for the matrix project that owns
    ``uri``, computing it on a cold cache. When ``uri`` is omitted, fall
    back to the workspace-root project (single-project case) or the only
    cached graph (legacy / tests).

    The cache is populated by the debounced ``_refresh_*`` helpers on every
    save/open. Hover/definition/completion handlers reuse it without paying
    for re-validation on every keystroke.
    """
    state = _state(server)
    project_root: Path | None = None

    if uri is not None:
        project_root = state.project_for_uri.get(uri)
        if project_root is None:
            workspace_root = _workspace_root(server)
            file_path = _path_from_uri(uri)
            if workspace_root is not None and file_path is not None:
                project_root = _find_project_root(file_path, workspace_root)
            if project_root is not None:
                state.project_for_uri[uri] = project_root

    # Cold cache: kick off a validation run.
    if project_root is not None and project_root not in state.cached_results:
        _refresh_project(server, project_root)
    elif project_root is None and not state.cached_results:
        _refresh_validation(server)

    if project_root is not None:
        result = state.cached_results.get(project_root)
        return result.assembled if result is not None else None

    # No URI / no project for it: legacy fallback. Use the workspace root
    # project when present, else the first cached project (covers tests
    # that don't go through didOpen).
    workspace_root = _workspace_root(server)
    if workspace_root is not None:
        result = state.cached_results.get(workspace_root)
        if result is not None:
            return result.assembled
    if state.cached_results:
        return next(iter(state.cached_results.values())).assembled
    return None


def _read_document(server: LanguageServer, uri: str) -> str | None:
    document = server.workspace.get_text_document(uri)
    return document.source if document else None


def _format_document(server: LanguageServer, uri: str) -> list[lsp.TextEdit] | None:
    """Format the entire document and return a single full-range TextEdit
    that replaces it. Returns ``None`` when the file extension isn't one
    we format, or when the formatter produced no change (so the editor
    doesn't dirty the buffer for a no-op)."""
    document = server.workspace.get_text_document(uri)
    if document is None:
        return None
    source = document.source
    suffix = _uri_to_path(uri).suffix.lower()
    if suffix == ".ttl":
        formatted = format_turtle(source)
    elif suffix in {".rq", ".sparql"}:
        formatted = format_sparql(source)
    else:
        return None
    if formatted == source:
        return None
    # Replace the entire document. End-line is the line count, end-char
    # is 0 — by spec this is "one past the last character" and covers
    # the trailing newline.
    line_count = source.count("\n")
    end_pos = lsp.Position(line=line_count, character=0)
    return [
        lsp.TextEdit(
            range=lsp.Range(start=lsp.Position(line=0, character=0), end=end_pos),
            new_text=formatted,
        )
    ]


# ---------------------------------------------------------------------------
# Prefixed-name extraction at a cursor position. The TTL / SPARQL syntax
# both use `prefix:localName` shapes — extract by walking left and right
# from the cursor until a non-name character.


# Characters that constitute a prefixed-name token. Excludes ``.`` because a TTL statement
# terminator (``ex:foo .``) would otherwise be swept into the local name, producing
# ``foo.`` as the extracted token and a failed resolution. Authors who use ``.`` legitimately
# inside a local name (uncommon in practice) will lose that affordance from the LSP only,
# not from the validation pipeline.
_NAME_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-:")


def _extract_prefixed_name_at(
    text: str, position: lsp.Position, prefix_table: dict[str, str]
) -> str | None:
    """Return the full URI of the prefixed name under the cursor, or None.

    Walks left and right from the cursor accumulating name chars, splits
    on the first ``:`` to get prefix vs localName, resolves prefix to a
    namespace via the supplied ``prefix_table``. Callers pass the table
    appropriate for the open document (matrix-scoped for ``.rq``,
    assembled-graph-scoped for ``.ttl``).
    """
    lines = text.splitlines()
    if position.line >= len(lines):
        return None
    line = lines[position.line]
    if position.character > len(line):
        return None

    start = position.character
    while start > 0 and line[start - 1] in _NAME_CHARS:
        start -= 1
    end = position.character
    while end < len(line) and line[end] in _NAME_CHARS:
        end += 1

    token = line[start:end]
    if ":" not in token:
        return None
    prefix, _, local = token.partition(":")
    if not local:
        return None
    namespace = prefix_table.get(prefix)
    if namespace is None:
        return None
    return str(namespace) + local


def _prefix_table_for_document(
    server: LanguageServer, doc_uri: str, graph: Graph
) -> dict[str, str]:
    """Return the prefix table the editor should resolve against for the
    document at ``doc_uri``.

    For ``.ttl`` files, use the assembled graph's global table (all
    matrix prefixes are merged there). For ``.rq`` files, walk up to the
    owning ``matrix.ttl`` and use its prefix bindings — that's the table
    the build inlines at expansion time, so completion/hover should
    reflect the same scope.
    """
    graph_table = {label: str(ns) for label, ns in graph.namespaces()}
    if not doc_uri.endswith(".rq"):
        return graph_table

    try:
        path = _uri_to_path(doc_uri)
    except Exception:  # noqa: BLE001 — malformed URIs leave global table in place
        return graph_table

    # Walk up parent directories until we find a `matrix.ttl`. The `.rq` lives anywhere
    # under that matrix's spec/, typically `spec/scripts/foo.rq`.
    matrix_ttl: Path | None = None
    for ancestor in [path.parent, *path.parents]:
        candidate = ancestor / "matrix.ttl"
        if candidate.is_file():
            matrix_ttl = candidate
            break
    if matrix_ttl is None:
        return graph_table

    import re

    try:
        text = matrix_ttl.read_text(encoding="utf-8")
    except OSError:
        return graph_table

    scoped: dict[str, str] = dict(
        re.findall(r"@prefix\s+([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*<([^>]+)>", text)
    )
    default_match = re.search(r"@prefix\s*:\s*<([^>]+)>", text)
    if default_match:
        scoped[""] = default_match.group(1)

    # Fall through to graph_table for anything not declared on the matrix; this keeps
    # widely-used vocabularies (rdf/rdfs/owl) resolvable even when the matrix's own ttl
    # didn't redeclare them.
    merged = dict(graph_table)
    merged.update(scoped)
    return merged


def _prefix_label_before_cursor(text: str, position: lsp.Position) -> str | None:
    """Return the prefix label immediately preceding a `:` at the cursor,
    or None. Used by completion to decide which namespace to autocomplete.
    """
    lines = text.splitlines()
    if position.line >= len(lines):
        return None
    line = lines[position.line]
    # Look backward from cursor for the form `prefix:`. The cursor must be
    # right after the colon (or after some local-name chars already typed).
    cursor = min(position.character, len(line))
    # Find the colon by walking backwards
    end = cursor
    while end > 0 and line[end - 1] in _NAME_CHARS and line[end - 1] != ":":
        end -= 1
    if end == 0 or line[end - 1] != ":":
        return None
    label_end = end - 1
    start = label_end
    while start > 0 and line[start - 1] in _NAME_CHARS and line[start - 1] != ":":
        start -= 1
    if start == label_end:
        return None
    return line[start:label_end]


# ---------------------------------------------------------------------------


def _locate_definition_site(
    server: LanguageServer,
    uri: str,
    graph: Graph,
) -> lsp.Location | None:
    """Find a file:line where ``uri`` is defined.

    Search order: every matrix's spec directory under the workspace root,
    then the active mode's deps cache subtree. This walks each TTL file
    looking for a line that introduces the URI as a subject. The match
    heuristic is "line begins with a token that resolves to this URI" via
    the file's own prefix bindings.

    Proper graph-source tracking (storing per-triple source paths during
    parse) is a future improvement; this grep-style locator handles the
    common author-authored TTL well.
    """
    workspace_root = _workspace_root(server)
    if workspace_root is None:
        return None
    if "#" in uri:
        namespace, _, local_name = uri.rpartition("#")
        namespace += "#"
    else:
        namespace, _, local_name = uri.rpartition("/")
        namespace += "/"
    if not local_name:
        return None

    for ttl_path in _candidate_ttl_paths(workspace_root):
        location = _find_subject_in_ttl(ttl_path, namespace, local_name)
        if location is not None:
            return location
    return None


def _candidate_ttl_paths(workspace_root: Path) -> Iterable[Path]:
    """Yield TTL paths to search for a definition: every matrix's spec
    directory in the project, then the active mode's deps cache subtree.
    """
    # Walk every directory containing TTL files under the workspace, but exclude the deps
    # cache root from this first pass — we want author-owned matrices to win over the
    # cached system matrix when both contain the term (the author's definition is the one
    # to navigate to). The deps cache then gets a second pass.
    deps_cache = workspace_root / ".matrix" / "deps"
    for ttl_path in workspace_root.rglob("*.ttl"):
        try:
            ttl_path.resolve().relative_to(deps_cache.resolve())
        except ValueError:
            # Outside the deps cache — this is author content. Yield it.
            yield ttl_path
            continue
    if deps_cache.is_dir():
        for ttl_path in deps_cache.rglob("*.ttl"):
            yield ttl_path


def _find_subject_in_ttl(
    ttl_path: Path, namespace: str, local_name: str
) -> lsp.Location | None:
    """Scan ``ttl_path`` for a line that begins with a token resolving to
    ``namespace + local_name`` via the file's own ``@prefix`` declarations.
    Returns the first match's location, or None.
    """
    try:
        text = ttl_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Build a label → namespace map from the file's @prefix declarations. Anything else
    # the file refers to via a prefix we cannot see is invisible; that's acceptable since
    # rdflib's parse-error path would already have surfaced the missing prefix elsewhere.
    import re

    prefix_decls = re.findall(
        r"@prefix\s+([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*<([^>]+)>", text
    )
    label_to_namespace = dict(prefix_decls)

    # Also support `@prefix : <ns>` (empty label = default).
    default_match = re.search(r"@prefix\s*:\s*<([^>]+)>", text)
    if default_match:
        label_to_namespace[""] = default_match.group(1)

    for line_no, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        if not stripped or stripped.startswith(("#", "@")):
            continue
        # The subject is the leading token up to whitespace.
        token = stripped.split(None, 1)[0]
        if ":" not in token:
            continue
        label, _, token_local = token.partition(":")
        # Strip TTL statement terminators (`;` / `,` / `.`) and brackets from the local.
        token_local = token_local.rstrip(";,.")
        if token_local != local_name:
            continue
        token_namespace = label_to_namespace.get(label)
        if token_namespace != namespace:
            continue
        return lsp.Location(
            uri=ttl_path.as_uri(),
            range=lsp.Range(
                start=lsp.Position(line=line_no, character=0),
                end=lsp.Position(line=line_no, character=len(line)),
            ),
        )
    return None


def _format_hover(description) -> str:
    """Markdown-format a TermDescription for the hover popup."""
    parts: list[str] = [f"**{description.uri}**"]
    if description.label:
        parts.append(f"_{description.label}_")
    if description.definition:
        parts.append(description.definition)
    elif description.comment:
        parts.append(description.comment)
    if description.types:
        parts.append("**type:** " + ", ".join(description.types))
    if description.subclass_of:
        parts.append("**subClassOf:** " + ", ".join(description.subclass_of))
    if description.defined_in:
        parts.append(f"**defined in:** {description.defined_in}")
    return "\n\n".join(parts)


__all__ = ["SERVER_NAME", "SERVER_VERSION", "create_server", "run_stdio"]


def _uri_to_path(uri: str) -> Path:
    """Convert a file:// URI to a local Path. Used in tests."""
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))
