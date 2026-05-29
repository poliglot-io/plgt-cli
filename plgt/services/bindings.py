"""Install-time variable & secret binding helpers.

Discovers `plgt-build:Variable` and `plgt-scrt:ManagedSecret` declarations from
a project's matrix TTL files (mirroring the registry's server-side filter so
that the CLI prompts for the same set of bindings the registry would expose),
resolves
QName/URI flag refs against project-declared prefixes, optionally prompts for
required declarations when stdin is a TTY, and encrypts secret values via the
existing platform `/pubkey` flow.

Bindings are optional at install time — the platform accepts an install with
no bindings and surfaces a WARNING event for any required declaration left
unset. The CLI mirrors that: empty input or non-TTY mode skips the binding
without raising, and a warning summary tells the user to set the slot via
the workspace afterwards.

Scope: local-project install only.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import typer
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF, RDFS
from rich.console import Console

from plgt.core.crypto import encrypt_secret_value
from plgt.core.exceptions import ServiceError, ValidationError
from plgt.services.build_service import discover_rdf_files_in_pattern

if TYPE_CHECKING:
    from pathlib import Path

    from plgt.core.sessions import APISession
    from plgt.models.build_types import PackageConfig

logger = logging.getLogger(__name__)

BUILD = Namespace("https://poliglot.io/os/spec/build#")
SECRETS = Namespace("https://poliglot.io/os/spec/secrets#")
MATRIX = Namespace("https://poliglot.io/os/spec/matrix#")


@dataclass(frozen=True)
class VariableDeclaration:
    """A `plgt-build:Variable` declared by a matrix in this project."""

    uri: str
    variable_type: str | None
    label: str | None
    description: str | None
    required: bool


@dataclass(frozen=True)
class SecretDeclaration:
    """A `plgt-scrt:ManagedSecret` declared by a matrix in this project."""

    uri: str
    label: str | None
    description: str | None
    required: bool


@dataclass
class ProjectDeclarations:
    """Aggregated declarations + prefix mapping parsed from a project's matrix TTLs."""

    variables: list[VariableDeclaration] = field(default_factory=list)
    secrets: list[SecretDeclaration] = field(default_factory=list)
    # Prefix → namespace URI, sourced from `@prefix` lines in matrix TTLs.
    prefixes: dict[str, str] = field(default_factory=dict)

    def variable_uris(self) -> set[str]:
        return {v.uri for v in self.variables}

    def secret_uris(self) -> set[str]:
        return {s.uri for s in self.secrets}


@dataclass
class RegistryDeclarations:
    """Declarations fetched from the registry for a published version.

    Mirrors :class:`ProjectDeclarations`'s ``variables``/``secrets`` shape but
    has no project prefix map — the registry-install flow has no local TTL to
    parse, so binding refs resolve via full URI or unambiguous localName
    suffix instead of QName.
    """

    variables: list[VariableDeclaration] = field(default_factory=list)
    secrets: list[SecretDeclaration] = field(default_factory=list)

    def variable_uris(self) -> set[str]:
        return {v.uri for v in self.variables}

    def secret_uris(self) -> set[str]:
        return {s.uri for s in self.secrets}


@dataclass
class VariableBinding:
    """A resolved variable binding ready to send to the install endpoint."""

    uri: str
    value: str
    source_matrix: str | None = None


@dataclass
class SecretBinding:
    """A resolved secret binding (plaintext) prior to E2E encryption."""

    uri: str
    value: str


@dataclass
class EncryptedSecretBinding:
    """E2E-encrypted secret binding ready for the install request body."""

    uri: str
    key_id: str
    client_public_key: str
    encrypted_value: str
    nonce: str


def discover_project_declarations(package_config: PackageConfig) -> ProjectDeclarations:
    """Parse the project's matrix TTL files and extract declared bindings.

    Mirrors the registry's server-side discovery so that a CLI install of a
    local project prompts for the same set of bindings the package would
    publish.

    Variables/secrets are filtered by `rdfs:isDefinedBy` pointing at one of
    this project's matrix URIs. Resources defined by imported (external)
    matrices are NOT included.
    """
    graph = Graph()
    prefixes: dict[str, str] = {}

    for matrix in package_config.matrices:
        matrix_dir = package_config.project_dir / matrix.path
        if not matrix_dir.exists():
            continue
        rdf_files: list[Path] = []
        for pattern in matrix.spec_patterns:
            rdf_files.extend(discover_rdf_files_in_pattern(matrix_dir, pattern))
        for rdf_file in rdf_files:
            sub_graph = Graph()
            # Let rdflib pick the format from the file extension. Mirrors how
            # `services.rdf_operations.validate_rdf_file` parses files in the
            # build flow.
            try:
                sub_graph.parse(str(rdf_file))
            except (OSError, ValueError, SyntaxError) as e:
                # rdflib raises ValueError/SyntaxError for malformed input and
                # OSError for unreadable files; both mean "skip this file" not
                # "abort the install".
                logger.debug("Failed to parse %s: %s", rdf_file, e)
                continue
            # Capture prefix declarations from this file.
            for prefix, ns in sub_graph.namespaces():
                if prefix and prefix not in prefixes:
                    prefixes[prefix] = str(ns)
            graph += sub_graph

    # Collect this project's matrix URIs by finding every subject typed
    # `plgt-mtx:Matrix`. Mirrors how the registry's server-side metadata
    # extraction identifies matrices, but without requiring a manifest.json
    # (we run pre-build).
    matrix_refs: set[URIRef] = set()
    for matrix_subject in graph.subjects(RDF.type, MATRIX.Matrix):
        if isinstance(matrix_subject, URIRef):
            matrix_refs.add(matrix_subject)

    declarations = ProjectDeclarations(prefixes=prefixes)

    for var in graph.subjects(RDF.type, BUILD.Variable):
        if not _defined_by_any(graph, var, matrix_refs):
            continue
        declarations.variables.append(
            VariableDeclaration(
                uri=str(var),
                variable_type=_str(graph.value(var, BUILD.variableType)),
                label=_str(graph.value(var, BUILD.label)),
                description=_str(graph.value(var, BUILD.description)),
                required=_bool(graph.value(var, BUILD.required), default=True),
            )
        )

    for sec in graph.subjects(RDF.type, SECRETS.ManagedSecret):
        if not _defined_by_any(graph, sec, matrix_refs):
            continue
        declarations.secrets.append(
            SecretDeclaration(
                uri=str(sec),
                label=_str(graph.value(sec, SECRETS.label)),
                description=_str(graph.value(sec, SECRETS.description)),
                required=_bool(graph.value(sec, SECRETS.required), default=True),
            )
        )

    declarations.variables.sort(key=lambda d: d.uri)
    declarations.secrets.sort(key=lambda d: d.uri)
    return declarations


def resolve_ref(ref: str, prefixes: dict[str, str]) -> str:
    """Resolve a flag ref (full URI or QName) to a full URI.

    QNames have the shape `prefix:localName` where `prefix` must be declared
    in the project's matrix TTLs. Anything containing `://` is treated as a
    full URI and returned as-is.

    Raises ``ValidationError`` if a QName references an undeclared prefix.
    """
    # Full URIs win even when they contain a colon (e.g. https://...).
    if "://" in ref:
        return ref
    if ":" not in ref:
        msg = f"binding ref '{ref}' must be a full URI or a 'prefix:localName' QName"
        raise ValidationError(msg)
    prefix, _, local = ref.partition(":")
    if prefix not in prefixes:
        msg = f"prefix '{prefix}' is not declared in this project's matrix files"
        raise ValidationError(msg)
    return f"{prefixes[prefix]}{local}"


def parse_var_flag(value: str) -> tuple[str, str]:
    """Split a `--var REF=VALUE` flag value. The first `=` is the separator."""
    if "=" not in value:
        msg = f"--var '{value}' must be in REF=VALUE form"
        raise ValidationError(msg)
    ref, _, val = value.partition("=")
    if not ref:
        msg = f"--var '{value}' has empty REF"
        raise ValidationError(msg)
    return ref, val


def parse_secret_from_env_flag(value: str) -> tuple[str, str]:
    """Split a `--secret-from-env REF=ENV_VAR` flag value."""
    if "=" not in value:
        msg = f"--secret-from-env '{value}' must be in REF=ENV_VAR form"
        raise ValidationError(msg)
    ref, _, env_var = value.partition("=")
    if not ref:
        msg = f"--secret-from-env '{value}' has empty REF"
        raise ValidationError(msg)
    if not env_var:
        msg = f"--secret-from-env '{value}' has empty ENV_VAR"
        raise ValidationError(msg)
    return ref, env_var


def resolve_registry_ref(ref: str, declarations: RegistryDeclarations) -> str:
    """Resolve a flag REF on the registry-install path.

    Accepts a full URI (must match a declared URI) or a localName suffix that
    uniquely identifies one declared variable or secret. QName form
    ``prefix:localName`` is rejected because the registry-install path has no
    project prefix map.

    Raises :class:`ValidationError` when the REF matches no declaration or is
    ambiguous (more than one declared URI ends in the same localName).
    """
    if "://" in ref:
        return ref
    if ":" in ref:
        msg = (
            f"binding ref '{ref}' is a QName, but registry installs have no "
            "project prefix map; use the full URI or a unique localName"
        )
        raise ValidationError(msg)

    candidates: list[str] = []
    for uri in (*declarations.variable_uris(), *declarations.secret_uris()):
        # Split on '#' first (matches RDF fragment-style namespaces), then '/'
        # so both `https://example.com/ns#Foo` and `https://example.com/ns/Foo`
        # extract `Foo` as the localName.
        local = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if local == ref:
            candidates.append(uri)
    if not candidates:
        msg = f"binding ref '{ref}' does not match any declared variable or secret"
        raise ValidationError(msg)
    if len(candidates) > 1:
        msg = (
            f"binding ref '{ref}' is ambiguous (matches: {sorted(candidates)}); "
            "use the full URI"
        )
        raise ValidationError(msg)
    return candidates[0]


def collect_bindings(
    declarations: ProjectDeclarations | RegistryDeclarations,
    var_flags: list[str],
    secret_env_flags: list[str],
    *,
    resolve: Any = None,
    is_tty: bool | None = None,
    prompt: Any = None,
    env: dict[str, str] | None = None,
    console: Console | None = None,
) -> tuple[list[VariableBinding], list[SecretBinding]]:
    """Resolve flags + prompts into a final binding set.

    All bindings are optional at install time — the platform accepts an
    install with no bindings and surfaces a WARNING event for any required
    declaration left unset. The CLI mirrors that contract: in TTY mode each
    declared variable/secret is prompted (Enter to skip); in non-TTY mode
    every prompt is skipped silently. After collection a single warning
    summary lists any required declarations the user left unset, with
    instructions to set them in the workspace.

    Args:
        declarations: Parsed project declarations.
        var_flags: Raw ``--var`` flag values (each ``REF=VALUE``).
        secret_env_flags: Raw ``--secret-from-env`` flag values
            (each ``REF=ENV_VAR``).
        is_tty: Whether stdin is a TTY (overrides ``sys.stdin.isatty()`` for
            tests). When False, prompts are skipped.
        prompt: Callable accepting ``(message, hide_input)`` that returns a
            user-supplied string. Defaults to a rich-backed prompt.
        env: Override for ``os.environ`` (used in tests).
        console: Rich console used to render the prompt headers and any
            skipped-binding warning. Defaults to a stderr console.

    Returns:
        ``(variable_bindings, secret_bindings)`` — entries the user actually
        supplied. Skipped declarations (whether optional or required) are
        omitted from both lists.

    Raises:
        ValidationError: Only for malformed flags — undeclared prefix,
            unknown URI, unset env var, duplicate URI. Missing required
            bindings are NOT raised.
    """
    if is_tty is None:
        is_tty = sys.stdin.isatty()
    if prompt is None:
        prompt = _default_prompt
    if env is None:
        env = dict(os.environ)
    if resolve is None:
        if isinstance(declarations, RegistryDeclarations):
            resolve = lambda ref: resolve_registry_ref(ref, declarations)  # noqa: E731
        else:
            resolve = lambda ref: resolve_ref(ref, declarations.prefixes)  # noqa: E731
    if console is None:
        console = Console(stderr=True)

    variable_uris = declarations.variable_uris()
    secret_uris = declarations.secret_uris()

    var_bindings: dict[str, VariableBinding] = {}
    secret_bindings: dict[str, SecretBinding] = {}

    for raw in var_flags:
        ref, val = parse_var_flag(raw)
        uri = resolve(ref)
        if uri not in variable_uris:
            msg = f"--var URI '{uri}' is not declared as a plgt-build:Variable in this package"
            raise ValidationError(msg)
        if uri in var_bindings:
            msg = f"--var '{uri}' was supplied more than once"
            raise ValidationError(msg)
        var_bindings[uri] = VariableBinding(uri=uri, value=val)

    for raw in secret_env_flags:
        ref, env_var = parse_secret_from_env_flag(raw)
        uri = resolve(ref)
        if uri not in secret_uris:
            msg = (
                f"--secret-from-env URI '{uri}' is not declared as a "
                f"plgt-scrt:ManagedSecret in this package"
            )
            raise ValidationError(msg)
        if uri in secret_bindings:
            msg = f"--secret-from-env '{uri}' was supplied more than once"
            raise ValidationError(msg)
        if env_var not in env or env[env_var] == "":
            msg = f"env var '{env_var}' is not set"
            raise ValidationError(msg)
        secret_bindings[uri] = SecretBinding(uri=uri, value=env[env_var])

    skipped: list[tuple[str, str, bool]] = []  # (label, uri, is_secret)

    # Only required declarations get an interactive prompt — optional ones
    # are left to flags. Empty input at the prompt skips the binding (the
    # user can set it in the workspace afterwards).
    for var in declarations.variables:
        if var.uri in var_bindings:
            continue
        if not var.required:
            continue
        label = var.label or _local_name(var.uri)
        if not is_tty:
            skipped.append((label, var.uri, False))
            continue
        _print_prompt_header(console, label, var.description, is_secret=False)
        value = prompt("Value", hide_input=False)
        if value == "":
            skipped.append((label, var.uri, False))
            continue
        var_bindings[var.uri] = VariableBinding(uri=var.uri, value=value)

    for sec in declarations.secrets:
        if sec.uri in secret_bindings:
            continue
        if not sec.required:
            continue
        label = sec.label or _local_name(sec.uri)
        if not is_tty:
            skipped.append((label, sec.uri, True))
            continue
        _print_prompt_header(console, label, sec.description, is_secret=True)
        value = prompt("Value", hide_input=True)
        if value == "":
            skipped.append((label, sec.uri, True))
            continue
        secret_bindings[sec.uri] = SecretBinding(uri=sec.uri, value=value)

    _print_skipped_warning(console, skipped)

    return list(var_bindings.values()), list(secret_bindings.values())


def encrypt_secret_bindings(
    session: APISession,
    workspace: str,
    bindings: list[SecretBinding],
) -> list[EncryptedSecretBinding]:
    """E2E-encrypt each secret binding via the platform `/pubkey` flow.

    One ephemeral key per secret binding (single-use constraint enforced by
    `EphemeralKeyCache`). Mirrors the encrypt step in
    ``SecretsClient.set_secret_value``.
    """
    encrypted: list[EncryptedSecretBinding] = []
    for binding in bindings:
        try:
            pubkey_response = session.post(
                f"/api/v1/secrets/{workspace}/pubkey",
            )
        except Exception as e:
            msg = f"Failed to fetch ephemeral key for secret '{binding.uri}': {e}"
            raise ServiceError(msg) from e
        pubkey_data = pubkey_response.json()
        if "data" in pubkey_data:
            pubkey_data = pubkey_data["data"]
        try:
            server_public_key = base64.b64decode(pubkey_data["serverPublicKey"])
            key_id = pubkey_data["keyId"]
            request, _ = encrypt_secret_value(binding.value, server_public_key)
        except (KeyError, ValueError) as e:
            msg = f"Failed to encrypt secret '{binding.uri}': {e}"
            raise ServiceError(msg) from e
        encrypted.append(
            EncryptedSecretBinding(
                uri=binding.uri,
                key_id=key_id,
                client_public_key=base64.b64encode(request.client_public_key).decode(
                    "ascii"
                ),
                encrypted_value=base64.b64encode(request.encrypted_value).decode(
                    "ascii"
                ),
                nonce=base64.b64encode(request.nonce).decode("ascii"),
            )
        )
    return encrypted


def _default_prompt(message: str, *, hide_input: bool) -> str:
    """Prompt for a value, echoing `*` per character when hide_input is set.

    For visible input (variables) we delegate to typer.prompt — click handles
    cursor, history, and editing. For hidden input (secrets) the standard
    getpass behavior shows literally nothing while typing, which leaves the
    user wondering whether their paste landed. We swap in a custom masked
    echo that prints `*` per character so the user sees length feedback.
    """
    if not hide_input:
        return typer.prompt(message, hide_input=False, default="", show_default=False)
    return _masked_input(f"{message}: ")


def _masked_input(prompt_text: str) -> str:
    """Read a line from stdin echoing `*` per character.

    Tries termios raw mode for character-by-character echo. If stdin isn't a
    TTY or termios is unavailable (Windows, weird shells), falls back to
    getpass so behavior degrades gracefully — same value, just no echo.

    Enables bracketed-paste mode (DECSET 2004) so multi-line pastes don't
    submit on the first newline. Pasted newlines (whether \\r, \\n, or
    \\r\\n) are normalized to \\n in the captured value. Pasted control
    characters are dropped — the visible mask still tracks the printable
    length so the user gets accurate feedback.
    """
    sys.stdout.write(prompt_text)
    sys.stdout.flush()

    try:
        import os
        import select
        import termios
        import tty
    except ImportError:
        # Non-Unix: fall back to getpass (no echo at all).
        from getpass import getpass

        return getpass("")

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        from getpass import getpass

        return getpass("")

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    chars: list[str] = []
    in_paste = False

    # Enable bracketed paste; disable in finally. Terminals that don't
    # support it ignore the SET/RESET sequences as no-ops.
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.flush()

    def _readch() -> str:
        """Read exactly one byte from the TTY as a 1-char latin-1 string.

        Bypasses sys.stdin's user-space buffering: BufferedReader will pull
        the entire kernel buffer on its first read(), leaving select() with
        nothing to see on subsequent calls and breaking our paste-marker
        detection. os.read goes straight to the fd, keeping select() and
        the read in sync.

        Latin-1 is a 1-byte-per-char codec — every byte maps cleanly to a
        char, so single-byte protocol logic (escape parsing, control-key
        detection) works regardless of UTF-8 multi-byte sequences. The
        captured value is reassembled as bytes and UTF-8 decoded at the
        end so multi-byte secrets round-trip correctly.
        """
        try:
            b = os.read(fd, 1)
        except OSError:
            return ""
        if not b:
            return ""
        return b.decode("latin-1")

    def _read_escape_seq() -> str:
        """Read one escape sequence's tail after an \\x1b has been consumed.

        Returns the bytes AFTER the leading ESC, bounded so we never eat
        past the sequence's terminator. CSI sequences (\\x1b[…) end on a
        byte in the 0x40-0x7E range, `~` for paste markers, alpha for
        arrow keys, etc. Lone ESC keypresses time out with empty return.
        """
        if not select.select([fd], [], [], 0.05)[0]:
            return ""  # Lone ESC — discard.
        intro = _readch()
        if intro != "[":
            # Not CSI (e.g. SS3 \x1bO<x>). Read one more byte if available.
            if select.select([fd], [], [], 0.01)[0]:
                return intro + _readch()
            return intro
        seq = "["
        while True:
            if not select.select([fd], [], [], 0.05)[0]:
                break
            c = _readch()
            seq += c
            if c and 0x40 <= ord(c) <= 0x7E:
                break
        return seq

    try:
        tty.setraw(fd)
        prev_was_cr = False  # track CR so we can collapse \r\n → \n in paste
        while True:
            ch = _readch()
            if not ch:
                break  # EOF

            # ─── Escape sequences (paste markers, arrow keys, etc.) ───
            if ch == "\x1b":
                prev_was_cr = False
                seq = ch + _read_escape_seq()
                if seq == "\x1b[200~":
                    in_paste = True
                    continue
                if seq == "\x1b[201~":
                    in_paste = False
                    continue
                # Unknown escape sequence inside paste → keep printable
                # parts as content. Outside paste (arrow keys etc.) →
                # ignore silently.
                if in_paste:
                    for c in seq:
                        if c.isprintable() or c in ("\n", "\t"):
                            chars.append(c)
                            sys.stdout.write("*")
                    sys.stdout.flush()
                continue

            # ─── Backspace (works in or out of paste) ─────────────────
            # Handled before the in_paste branch so a stuck in_paste flag
            # doesn't make backspace silently dead. Also lets the user
            # correct an over-pasted value before submitting.
            if ch in ("\x7f", "\b"):
                prev_was_cr = False
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue

            # ─── Ctrl+C / Ctrl+D (never get swallowed by paste mode) ──
            # Same logic as backspace — process control keys regardless of
            # in_paste state so the user can always abort.
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch == "\x04":
                # Submit whatever was typed so far (empty == skip).
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break

            # ─── Inside paste: newlines are content, never submit ─────
            if in_paste:
                if ch == "\n" and prev_was_cr:
                    # CRLF was already counted as one \n; swallow the \n.
                    prev_was_cr = False
                    continue
                if ch == "\r":
                    chars.append("\n")
                    sys.stdout.write("*")
                    sys.stdout.flush()
                    prev_was_cr = True
                    continue
                prev_was_cr = False
                if ch.isprintable() or ch in ("\n", "\t"):
                    chars.append(ch)
                    sys.stdout.write("*")
                    sys.stdout.flush()
                continue

            # ─── Outside paste: interactive line editing ──────────────
            prev_was_cr = False
            if ch in ("\r", "\n"):
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                break
            if ch.isprintable():
                chars.append(ch)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Each entry in `chars` is a 1-byte latin-1 char (see _readch). Round
    # trip via bytes → utf-8 so multi-byte input survives.
    return "".join(chars).encode("latin-1").decode("utf-8", errors="replace")


def _local_name(uri: str) -> str:
    """Best-effort local name extraction for a URI.

    Falls back to the full URI when no fragment/path separator is present.
    """
    for sep in ("#", "/"):
        idx = uri.rfind(sep)
        if idx >= 0 and idx < len(uri) - 1:
            return uri[idx + 1 :]
    return uri


def _print_prompt_header(
    console: Console,
    label: str,
    description: str | None,
    *,
    is_secret: bool,
) -> None:
    """Render a clean header above each binding prompt.

    Shows the human label and optional description on separate lines so the
    `Value:` prompt itself stays short — the URI is intentionally omitted
    so a long URI can't push the input cursor off-screen.
    """
    kind = "secret" if is_secret else "variable"
    hint = "  [dim]press Enter to skip and set later in workspace[/dim]"
    console.print()
    console.print(f"[bold cyan]{label}[/bold cyan] [dim]({kind})[/dim]")
    if description:
        console.print(f"  [dim]{description}[/dim]")
    console.print(hint)


def _print_skipped_warning(
    console: Console,
    skipped: list[tuple[str, str, bool]],
) -> None:
    """Warn about required declarations the user left unset.

    Install proceeds — the platform accepts these as warning-only — but the
    user needs the URIs to know which workspace slots to fill in afterwards.
    """
    if not skipped:
        return
    console.print()
    console.print(
        "[yellow]⚠ Required bindings left unset — install will proceed but"
        " these need values before the package can run:[/yellow]"
    )
    for label, uri, is_secret in skipped:
        kind = "secret" if is_secret else "variable"
        console.print(f"  • [bold]{label}[/bold] [dim]({kind})[/dim] [dim]{uri}[/dim]")
    console.print(
        "[dim]Set them in your workspace via the variables/secrets UI or"
        " `plgt secrets set` / `plgt variables set`.[/dim]"
    )


def _defined_by_any(graph: Graph, subject: URIRef, allowed: set[URIRef]) -> bool:
    return any(o in allowed for o in graph.objects(subject, RDFS.isDefinedBy))


def _str(node: Any) -> str | None:
    if node is None:
        return None
    return str(node)


def _bool(node: Any, default: bool) -> bool:
    if node is None:
        return default
    s = str(node).strip().lower()
    return s in ("true", "1")
