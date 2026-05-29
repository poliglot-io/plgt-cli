"""Opinionated SPARQL formatter — canonical output for canonical input.

Rules (frozen):

* **Keyword case**: UPPER for SPARQL keywords.
* **Indent**: 4 spaces. Each ``{ ... }`` group adds one level.
* **PREFIX block**: top, alphabetised. Two blank lines after.
* **``{`` placement**: same line as the introducing keyword.
* **``}`` placement**: own line at parent's indent.
* **Triple statement**: subject + predicate-list + ``.`` on one (or
  ``;``-continued multi-line) statement.
* **Blank line after every ``.``**: every full triple terminator gets
  exactly one blank line (the BGP-component rule).
* **Non-triple elements** (FILTER/OPTIONAL/BIND/UNION/MINUS/GRAPH/VALUES/
  sub-SELECT): preceded and followed by a blank line.
* **Variables**: ``?var`` only (``$var`` normalised by the lexer).
* **Comments**: preserved at position.
* **Trailing newline**: exactly one.

JSON-DSL detection: if the source contains a ``JSON { ... } WHERE { ... }``
outer shape, only the ``WHERE`` block is reformatted; the JSON payload is
left verbatim.
"""

from __future__ import annotations

from plgt.services.formatter.lexer import TT, Token, tokenize

INDENT = "    "
MAX_LINE = 100

NON_TRIPLE_KEYWORDS = frozenset(
    {"FILTER", "OPTIONAL", "MINUS", "BIND", "VALUES", "SERVICE", "UNION"}
)

# Width at which we stop trying to keep something on one line. Generous
# because SPARQL identifiers + IRIs run long; we still prefer inline for
# readability when the body is short enough.
INLINE_FOLD_MAX = 100


def _grab_balanced(
    tokens: list[Token], i: int, open_t: TT, close_t: TT
) -> tuple[list[Token], int]:
    """Return (slice_through_close, index_after_close) for the balanced
    bracketed range starting at ``tokens[i]`` (which must be ``open_t``).
    On unbalanced input returns the rest of the list with len(tokens)."""
    depth = 0
    j = i
    while j < len(tokens):
        if tokens[j].type == open_t:
            depth += 1
        elif tokens[j].type == close_t:
            depth -= 1
            if depth == 0:
                return tokens[i : j + 1], j + 1
        j += 1
    return tokens[i:], len(tokens)


def _is_complex_bracket(slice_: list[Token]) -> bool:
    """A bracketed range (`[...]` or `(...)`) is *complex* — and therefore
    deserves multi-line layout — when it contains either:

    * a ``;`` at the body's depth (multi-predicate property list), or
    * a nested ``[`` or ``(`` that is itself complex, or
    * its inline render would exceed ``INLINE_FOLD_MAX`` columns.

    The "nested-and-itself-complex" recursion is what handles things like
    ``flow:newParameters ( [ ... ] [ ... ] )`` — a single ``(`` that wraps
    two inline ``[]`` members is *not* complex (each member fits), but if
    a member's property list is long the outer ``(`` flips to complex.
    """
    if not slice_ or len(slice_) < 2:
        return False
    body = slice_[1:-1]
    depth = 0
    for tok in body:
        if tok.type == TT.SEMI and depth == 0:
            return True
        if tok.type in (TT.LBRACK, TT.LPAREN):
            depth += 1
        elif tok.type in (TT.RBRACK, TT.RPAREN):
            depth -= 1
    # Recurse: nested brackets that are themselves complex.
    j = 0
    while j < len(body):
        if body[j].type == TT.LBRACK:
            sub, j2 = _grab_balanced(body, j, TT.LBRACK, TT.RBRACK)
            if _is_complex_bracket(sub):
                return True
            j = j2
        elif body[j].type == TT.LPAREN:
            sub, j2 = _grab_balanced(body, j, TT.LPAREN, TT.RPAREN)
            if _is_complex_bracket(sub):
                return True
            j = j2
        else:
            j += 1
    # Inline-length budget.
    return _inline_token_render_length(slice_) > INLINE_FOLD_MAX


def _inline_token_render_length(slice_: list[Token]) -> int:
    """Approximate width of ``slice_`` if rendered inline with the
    spacing rules used inside a triple. Used by ``_is_complex_bracket``
    to decide multi-line layout."""
    return len(_render_tokens_inline(slice_))


def _render_tokens_inline(slice_: list[Token]) -> str:
    """Render a flat token list as if it were a single inline term —
    applying the same spacing rules as ``_emit_triple``. Returns the
    rendered string with no leading or trailing whitespace.

    This is the spacing arbiter for both the inline-fold decision (we
    use the resulting length to decide multi-line) and for actually
    emitting an inline-fit bracketed term.
    """
    out: list[str] = []
    prev_type: TT | None = None
    prev_text: str = ""
    operator_keywords = {"IN", "NOT"}
    for tok in slice_:
        if tok.type == TT.LPAREN:
            tight = (
                prev_type
                in (
                    TT.PNAME,
                    TT.KEYWORD,
                    TT.IDENT,
                    TT.RPAREN,
                )
                and prev_text not in operator_keywords
            )
            if prev_type is None or tight:
                out.append("(")
            else:
                out.append(" (")
        elif tok.type == TT.RPAREN:
            out.append(")")
        elif tok.type == TT.LBRACK:
            # Inline `[ ... ]` always gets a single space on either side
            # of its body so it reads as a unit, matching Turtle convention.
            if prev_type is not None:
                out.append(" [")
            else:
                out.append("[")
        elif tok.type == TT.RBRACK:
            out.append(" ]")
        elif tok.type == TT.PATH_OP:
            out.append(tok.text)
        elif tok.type == TT.COMMA:
            out.append(",")
            # space added by the next token (or absent if RPAREN follows)
        elif tok.type == TT.SEMI:
            out.append(" ;")
        elif tok.type == TT.DOT:
            out.append(" .")
        else:
            # Default token. Add a leading space unless the previous
            # token was a tight-binding opener (LPAREN of a function
            # call, property-path operator). LBRACK is NOT tight — `[ pred ]`
            # reads as a unit and the space after `[` is part of the
            # property-list convention.
            if prev_type is not None and prev_type not in (
                TT.LPAREN,
                TT.PATH_OP,
            ):
                out.append(" ")
            out.append(tok.text)
        prev_type = tok.type
        prev_text = tok.text
    return "".join(out).strip()


def _try_inline_group(tokens: list[Token], i: int) -> tuple[str, int] | None:
    """If the group starting at ``tokens[i]`` (which must be ``LBRACE``)
    is "simple enough" to fold inline, return (rendered, index_after_close).
    Else return None.

    Simple = no nested groups, no non-triple keywords (FILTER/OPTIONAL/…),
    no ``;`` (one predicate-list), no comments to preserve, and an inline
    render that fits ``INLINE_FOLD_MAX``.
    """
    if i >= len(tokens) or tokens[i].type != TT.LBRACE:
        return None
    slice_, end = _grab_balanced(tokens, i, TT.LBRACE, TT.RBRACE)
    body = slice_[1:-1]
    if not body:
        return "{ }", end
    for tok in body:
        if tok.type in (TT.LBRACE, TT.RBRACE):
            return None
        if tok.type == TT.SEMI:
            return None
        if tok.type == TT.COMMENT:
            return None
        if tok.type == TT.KEYWORD and tok.text in (
            NON_TRIPLE_KEYWORDS | {"GRAPH", "SELECT", "CONSTRUCT", "ASK", "DESCRIBE"}
        ):
            return None
    inline = "{ " + _render_tokens_inline(body) + " }"
    if len(inline) > INLINE_FOLD_MAX:
        return None
    return inline, end


def _same_subject(a: list[Token], b: list[Token]) -> bool:
    """Two subject token sequences are the same when their text
    representations match. Conservative: only handles single-token
    subjects (PNAME / VARIABLE / IRI / BLANK), which covers the vast
    majority of practical inputs. Bracketed subjects (`[]` / `()`) are
    deliberately not chained — chaining there would change semantics."""
    if len(a) != 1 or len(b) != 1:
        return False
    if a[0].type not in (TT.PNAME, TT.VARIABLE, TT.IRI, TT.BLANK):
        return False
    if b[0].type not in (TT.PNAME, TT.VARIABLE, TT.IRI, TT.BLANK):
        return False
    return a[0].text == b[0].text


def _find_matching_rbrace(tokens: list[Token], i: int) -> int | None:
    """Starting at the position *after* an opening LBRACE, walk forward
    to find the matching RBRACE at the same nesting depth. Returns the
    RBRACE's index, or None if unbalanced (the caller falls back to a
    pass-through, which renders the un-chained version)."""
    depth = 1
    j = i
    while j < len(tokens):
        if tokens[j].type == TT.LBRACE:
            depth += 1
        elif tokens[j].type == TT.RBRACE:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return None


def _chain_same_subject_triples(
    tokens: list[Token], start: int, end: int
) -> list[Token]:
    """Walk the slice ``tokens[start:end]`` looking for consecutive triple
    statements that share the same subject. Rewrite the token sequence so
    that ``A p1 o1 . A p2 o2 . A p3 o3 .`` becomes
    ``A p1 o1 ; p2 o2 ; p3 o3 .`` — the formatter then emits this as a
    single chained statement.

    Triple boundaries are identified by ``.`` at brace-depth 0 and
    paren-depth 0. Subjects are the token run from start-of-statement up
    to the first non-subject token (we accept only single-token subjects;
    see ``_same_subject``)."""
    out: list[Token] = []
    body = tokens[start:end]
    # First, segment by `.` at depth 0.
    statements: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    paren_d = 0
    for tok in body:
        if tok.type in (TT.LBRACE, TT.LBRACK):
            depth += 1
        elif tok.type in (TT.RBRACE, TT.RBRACK):
            depth -= 1
        elif tok.type == TT.LPAREN:
            paren_d += 1
        elif tok.type == TT.RPAREN:
            paren_d -= 1
        if tok.type == TT.DOT and depth == 0 and paren_d == 0:
            statements.append([*current, tok])
            current = []
            continue
        current.append(tok)
    if current:
        statements.append(current)

    # Now merge runs of statements with the same single-token subject.
    merged: list[list[Token]] = []
    for stmt in statements:
        if not stmt:
            continue
        # Only consider statements that look like a triple (subject is a
        # single named term followed by predicate(s)). Skip anything else
        # (comments, keyword-led non-triples we don't classify here, etc.)
        if stmt[0].type not in (TT.PNAME, TT.VARIABLE, TT.IRI, TT.BLANK):
            merged.append(stmt)
            continue
        subj = [stmt[0]]
        if merged and merged[-1]:
            prev = merged[-1]
            # Previous must end with DOT, and its subject must match.
            if (
                prev[-1].type == TT.DOT
                and prev[0].type in (TT.PNAME, TT.VARIABLE, TT.IRI, TT.BLANK)
                and _same_subject([prev[0]], subj)
            ):
                # Replace prev's trailing DOT with SEMI, drop the
                # duplicate subject from stmt, append stmt's body
                # (predicate + object … + DOT).
                prev[-1] = Token(
                    TT.SEMI,
                    ";",
                    prev[-1].line,
                    prev[-1].col,
                    prev[-1].leading_blank_lines,
                )
                merged[-1] = prev + stmt[1:]
                continue
        merged.append(stmt)

    for stmt in merged:
        out.extend(stmt)
    return tokens[:start] + out + tokens[end:]


def format_sparql(source: str) -> str:
    """Reformat SPARQL source. JSON-DSL auto-detected (only the WHERE
    block is reformatted in that case). On lexer error returns the input
    unchanged."""
    if _looks_like_json_dsl(source):
        return _format_json_dsl(source)
    return _format_pure_sparql(source)


def _looks_like_json_dsl(source: str) -> bool:
    """Conservative: a ``JSON`` keyword appears before any
    SELECT/CONSTRUCT/ASK/DESCRIBE/INSERT/DELETE."""
    try:
        tokens = tokenize(source, sparql_mode=True)
    except ValueError:
        return False
    for tok in tokens:
        if tok.type == TT.KEYWORD and tok.text == "JSON":
            return True
        if tok.type == TT.KEYWORD and tok.text in {
            "SELECT",
            "CONSTRUCT",
            "ASK",
            "DESCRIBE",
            "INSERT",
            "DELETE",
        }:
            return False
    return False


def _format_json_dsl(source: str) -> str:
    """Format a JSON-DSL body. Leaves the JSON payload verbatim; reformats
    the trailing WHERE block per SPARQL rules."""
    try:
        tokens = tokenize(source, sparql_mode=True)
    except ValueError:
        return source

    out = _Emitter()
    i = 0
    directives, i = _collect_directives(tokens, i)
    _emit_directives(out, directives)
    if directives and i < len(tokens):
        out.blank_lines(2)

    # Pass through anything before the JSON keyword as comments only.
    while i < len(tokens) and not (
        tokens[i].type == TT.KEYWORD and tokens[i].text == "JSON"
    ):
        if tokens[i].type == TT.COMMENT:
            out.line(tokens[i].text)
        i += 1
    if i >= len(tokens):
        return source

    json_start_offset = _token_source_offset(source, tokens[i])
    where_idx = _find_where_after_json(tokens, i)
    if where_idx is None:
        return source
    where_offset = _token_source_offset(source, tokens[where_idx])
    json_payload = source[json_start_offset:where_offset].rstrip()
    # Dedent the JSON payload so its base indent is column 0. The .rq
    # files were extracted from indented TTL bodies, so they often carry
    # 16+ columns of leading whitespace that we must normalise away.
    json_payload = _dedent_block(json_payload)
    payload_lines = json_payload.splitlines()
    # Append " WHERE {" inline with the JSON payload's closing brace so the
    # canonical shape is `... } WHERE {` on one line.
    if payload_lines and payload_lines[-1].rstrip().endswith("}"):
        last = payload_lines[-1].rstrip()
        for line in payload_lines[:-1]:
            out.line(line)
        out.line(f"{last} WHERE {{")
        # Skip the WHERE keyword and its opening brace in the token stream.
        j = where_idx + 1
        # Skip whitespace/comments to land on the LBRACE.
        while j < len(tokens) and tokens[j].type == TT.COMMENT:
            j += 1
        if j < len(tokens) and tokens[j].type == TT.LBRACE:
            j += 1
        _emit_group_body(tokens, j, out, indent=0)
    else:
        for line in payload_lines:
            out.line(line)
        _emit_clause(tokens, where_idx, out, indent=0)
    return out.finish()


def _dedent_block(text: str) -> str:
    """Strip the common leading-whitespace prefix from every line of
    ``text``. The first line is treated specially: it may have NO leading
    whitespace (because it starts immediately after ``JSON``) while
    subsequent lines have the surrounding indent. We compute the common
    prefix across lines 2..N (ignoring blank lines) and strip it from
    every line that has it; line 1 stays as-is."""
    lines = text.splitlines()
    if len(lines) <= 1:
        return text.lstrip(" \t")
    body = [ln for ln in lines[1:] if ln.strip()]
    if not body:
        return text
    common = min(len(ln) - len(ln.lstrip(" \t")) for ln in body)
    if common == 0:
        return text
    prefixes = (" " * common, "\t" * common)
    out = [lines[0]]
    for line in lines[1:]:
        if line.startswith(prefixes):
            out.append(line[common:])
        else:
            out.append(line)
    return "\n".join(out)


def _token_source_offset(source: str, token: Token) -> int:
    """Resolve a 1-based (line, col) Token back to a 0-based source
    offset. Used by JSON-DSL splitting to preserve source bytes
    verbatim between two token positions."""
    offset = 0
    line = 1
    while line < token.line:
        nl = source.find("\n", offset)
        if nl == -1:
            return offset
        offset = nl + 1
        line += 1
    return offset + token.col - 1


def _find_where_after_json(tokens: list[Token], i: int) -> int | None:
    depth = 0
    j = i + 1
    while j < len(tokens):
        tok = tokens[j]
        if tok.type == TT.LBRACE:
            depth += 1
        elif tok.type == TT.RBRACE:
            depth -= 1
            if depth == 0:
                k = j + 1
                while k < len(tokens) and tokens[k].type == TT.COMMENT:
                    k += 1
                if (
                    k < len(tokens)
                    and tokens[k].type == TT.KEYWORD
                    and tokens[k].text == "WHERE"
                ):
                    return k
                return None
        j += 1
    return None


def _format_pure_sparql(source: str) -> str:
    try:
        tokens = tokenize(source, sparql_mode=True)
    except ValueError:
        return source
    out = _Emitter()
    i = 0
    directives, i = _collect_directives(tokens, i)
    _emit_directives(out, directives)
    if directives and i < len(tokens):
        out.blank_lines(2)
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.COMMENT:
            out.line(tok.text)
            i += 1
            continue
        i = _emit_clause(tokens, i, out, indent=0)
    return out.finish()


# ---------------------------------------------------------------------------
# Emitter (mirrors the Turtle one — kept independent for clean cohesion).


class _Emitter:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._current: list[str] = []

    def write(self, text: str) -> None:
        self._current.append(text)

    def newline(self) -> None:
        self._lines.append("".join(self._current))
        self._current = []

    def line(self, text: str) -> None:
        if self._current:
            self.newline()
        self._lines.append(text)

    def blank_lines(self, n: int) -> None:
        if self._current:
            self.newline()
        while self._lines and self._lines[-1] == "":
            self._lines.pop()
        for _ in range(n):
            self._lines.append("")

    def current_line_len(self) -> int:
        return sum(len(s) for s in self._current)

    def current_tail(self) -> str:
        return self._current[-1] if self._current else ""

    def finish(self) -> str:
        if self._current:
            self.newline()
        while self._lines and self._lines[-1] == "":
            self._lines.pop()
        return "\n".join(self._lines) + "\n"


# ---------------------------------------------------------------------------
# Directives.


def _collect_directives(
    tokens: list[Token], i: int
) -> tuple[list[tuple[Token, list[Token]]], int]:
    directives: list[tuple[Token, list[Token]]] = []
    leading: list[Token] = []
    leading_start = i
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.COMMENT:
            leading.append(tok)
            i += 1
            continue
        if tok.type in (TT.PREFIX_DECL, TT.BASE_DECL):
            trailing: list[Token] = []
            if (
                i + 1 < len(tokens)
                and tokens[i + 1].type == TT.COMMENT
                and tokens[i + 1].line == tok.line
            ):
                trailing.append(tokens[i + 1])
                i += 1
            directives.append((tok, leading + trailing))
            leading = []
            i += 1
            leading_start = i
            continue
        break
    if leading:
        i = leading_start
    return directives, i


def _emit_directives(
    out: _Emitter, directives: list[tuple[Token, list[Token]]]
) -> None:
    base = [d for d in directives if d[0].type == TT.BASE_DECL]
    prefixes = [d for d in directives if d[0].type == TT.PREFIX_DECL]
    prefixes.sort(key=lambda d: _prefix_label(d[0].text))
    for tok, comments in base + prefixes:
        leading_cmt = [c for c in comments if c.line != tok.line]
        same_line = [c for c in comments if c.line == tok.line]
        for c in leading_cmt:
            out.line(c.text)
        body = _canonicalise_prefix(tok.text)
        if same_line:
            out.line(f"{body}  {same_line[0].text}")
        else:
            out.line(body)


def _canonicalise_prefix(text: str) -> str:
    parts = text.split()
    if parts and parts[0].upper() in {"PREFIX", "BASE"}:
        parts[0] = parts[0].upper()
    return " ".join(parts)


def _prefix_label(text: str) -> str:
    parts = text.split(None, 1)
    if len(parts) < 2:
        return ""
    rest = parts[1]
    colon = rest.find(":")
    if colon == -1:
        return ""
    return rest[:colon].strip()


# ---------------------------------------------------------------------------
# Clauses.


def _emit_clause(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    pad = INDENT * indent
    tok = tokens[i]
    if tok.type == TT.KEYWORD and tok.text in {
        "SELECT",
        "ASK",
        "CONSTRUCT",
        "DESCRIBE",
    }:
        return _emit_query_form(tokens, i, out, indent=indent)
    if tok.type == TT.KEYWORD and tok.text in {"INSERT", "DELETE", "WITH"}:
        return _emit_update_form(tokens, i, out, indent=indent)
    if tok.type == TT.KEYWORD and tok.text == "WHERE":
        out.write("WHERE ")
        i += 1
        return _emit_group(tokens, i, out, indent=indent)
    # Fallback: dump verbatim.
    out.line(pad + tok.text)
    return i + 1


def _emit_query_form(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    """SELECT ?a ?b WHERE { ... } ORDER BY ?a LIMIT 10."""
    pad = INDENT * indent
    out.write(pad)
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.LBRACE:
            # Single space before the brace so we never get `WHERE{`.
            if out.current_tail() and not out.current_tail().endswith(" "):
                out.write(" ")
            out.write("{")
            out.newline()
            i = _emit_group_body(tokens, i + 1, out, indent=indent)
            break
        if tok.type == TT.COMMENT:
            out.write(f"  {tok.text}")
            out.newline()
            out.write(pad)
            i += 1
            continue
        if out.current_line_len() > len(pad):
            out.write(" ")
        out.write(tok.text)
        i += 1
    # Solution modifiers.
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.COMMENT:
            out.line(pad + tok.text)
            i += 1
            continue
        if tok.type == TT.KEYWORD and tok.text in {
            "ORDER",
            "GROUP",
            "HAVING",
            "LIMIT",
            "OFFSET",
            "VALUES",
        }:
            i = _emit_modifier(tokens, i, out, indent=indent)
            continue
        break
    return i


def _emit_update_form(
    tokens: list[Token], i: int, out: _Emitter, *, indent: int
) -> int:
    pad = INDENT * indent
    out.write(pad)
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.LBRACE:
            if out.current_tail() and not out.current_tail().endswith(" "):
                out.write(" ")
            out.write("{")
            out.newline()
            i = _emit_group_body(tokens, i + 1, out, indent=indent)
            # Look ahead for a continuation clause (another INSERT/DELETE/WHERE).
            j = i
            while j < len(tokens) and tokens[j].type == TT.COMMENT:
                j += 1
            if (
                j < len(tokens)
                and tokens[j].type == TT.KEYWORD
                and tokens[j].text in {"WHERE", "INSERT", "DELETE"}
            ):
                # Continuation clause on the next line at the same indent —
                # _emit_group_body already wrote the close brace on its own
                # line, so we start a fresh line here without a blank one.
                out.write(pad)
                i = j
                continue
            break
        if tok.type == TT.COMMENT:
            out.write(f"  {tok.text}")
            out.newline()
            out.write(pad)
            i += 1
            continue
        if out.current_line_len() > len(pad):
            out.write(" ")
        out.write(tok.text)
        i += 1
    return i


def _emit_modifier(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    pad = INDENT * indent
    out.line(pad + tokens[i].text)
    i += 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.KEYWORD and tok.text in {
            "ORDER",
            "GROUP",
            "HAVING",
            "LIMIT",
            "OFFSET",
            "VALUES",
        }:
            return i
        if tok.type == TT.COMMENT:
            out.write(f"  {tok.text}")
            out.newline()
            return i + 1
        out.write(" " + tok.text)
        i += 1
    out.newline()
    return i


def _emit_group(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    if i >= len(tokens) or tokens[i].type != TT.LBRACE:
        return i
    # Try inline-fold first — single trivial pattern collapses to one line.
    inline = _try_inline_group(tokens, i)
    if inline is not None:
        text, end = inline
        if out.current_tail() and not out.current_tail().endswith(" "):
            out.write(" ")
        out.write(text)
        out.newline()
        return end
    if out.current_tail() and not out.current_tail().endswith(" "):
        out.write(" ")
    out.write("{")
    out.newline()
    return _emit_group_body(tokens, i + 1, out, indent=indent)


def _emit_group_body(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    body_indent = indent + 1
    pad = INDENT * body_indent
    # Track previous emitted element type so we can decide blank-line
    # placement.
    last_was_triple = False
    last_was_blank_eligible = False

    # Pre-process: collapse runs of consecutive triples with the same
    # subject into ;-chained statements. The pre-processing returns a
    # LOCAL token list of possibly different length; we walk it for
    # emission but must return the CALLER's index (one past the
    # caller-tokens RBRACE), so we cache that here.
    caller_rbrace_idx = _find_matching_rbrace(tokens, i)
    if caller_rbrace_idx is not None:
        tokens = _chain_same_subject_triples(tokens, i, caller_rbrace_idx)
        # The chained version is shorter when chaining happened. The new
        # RBRACE position is what we walk to; the return index is the
        # caller's RBRACE + 1.
        return_index = caller_rbrace_idx + 1
    else:
        return_index = None

    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.RBRACE:
            out.line(INDENT * indent + "}")
            # If we pre-chained tokens, the caller's index is one past
            # the original RBRACE — return that. Otherwise we're walking
            # the caller's list directly and `i + 1` is correct.
            return return_index if return_index is not None else i + 1
        if tok.type == TT.COMMENT:
            if last_was_triple:
                out.blank_lines(1)
            out.line(pad + tok.text)
            last_was_triple = False
            last_was_blank_eligible = True
            i += 1
            continue
        if tok.type == TT.KEYWORD and tok.text in NON_TRIPLE_KEYWORDS:
            if last_was_triple or last_was_blank_eligible:
                out.blank_lines(1)
            i = _emit_non_triple(tokens, i, out, indent=body_indent)
            last_was_triple = False
            last_was_blank_eligible = True
            continue
        if tok.type == TT.KEYWORD and tok.text == "GRAPH":
            if last_was_triple or last_was_blank_eligible:
                out.blank_lines(1)
            i = _emit_graph(tokens, i, out, indent=body_indent)
            last_was_triple = False
            last_was_blank_eligible = True
            continue
        if tok.type == TT.LBRACE:
            if last_was_triple or last_was_blank_eligible:
                out.blank_lines(1)
            out.write(pad)
            i = _emit_group(tokens, i, out, indent=body_indent)
            last_was_triple = False
            last_was_blank_eligible = True
            continue
        if tok.type == TT.KEYWORD and tok.text == "SELECT":
            if last_was_triple or last_was_blank_eligible:
                out.blank_lines(1)
            i = _emit_query_form(tokens, i, out, indent=body_indent)
            last_was_triple = False
            last_was_blank_eligible = True
            continue
        # Triple pattern. Blank line before it if the previous emitted
        # element was a triple (BGP-component rule) OR a non-triple
        # element (FILTER/OPTIONAL/BIND/...) — both isolate the triple.
        if last_was_triple or last_was_blank_eligible:
            out.blank_lines(1)
        i = _emit_triple(tokens, i, out, indent=body_indent)
        last_was_triple = True
        last_was_blank_eligible = False

    return i


def _emit_triple(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    """Emit one triple statement up to the closing DOT (or a group
    boundary — ``}`` — when the trailing dot is omitted, which SPARQL
    permits).

    When the triple contains a complex bracketed term — ``[...]`` with a
    multi-predicate property list, or ``(...)`` with one or more complex
    members — that term is rendered multi-line, indented one level
    deeper than the subject. Trivial brackets stay inline."""
    pad = INDENT * indent
    out.write(pad)
    started = False
    paren_depth = 0
    prev_type: TT | None = None
    prev_text: str = ""
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.RBRACE and paren_depth == 0:
            out.newline()
            return i
        if tok.type == TT.DOT and paren_depth == 0:
            out.write(" .")
            out.newline()
            return i + 1
        if tok.type == TT.COMMENT:
            out.write(f"  {tok.text}")
            out.newline()
            out.write(pad)
            started = False
            prev_type = None
            prev_text = ""
            i += 1
            continue
        if tok.type == TT.SEMI and paren_depth == 0:
            out.write(" ;")
            out.newline()
            out.write(INDENT * (indent + 1))
            started = False
            prev_type = None
            prev_text = ""
            i += 1
            continue
        if tok.type == TT.LBRACK:
            # Blank-node `[ ... ]` as an object term. Inline when trivial;
            # multi-line when complex (multi-predicate property-list, or
            # contains nested complex brackets, or > INLINE_FOLD_MAX).
            brack, end = _grab_balanced(tokens, i, TT.LBRACK, TT.RBRACK)
            if _is_complex_bracket(brack):
                if started:
                    out.write(" ")
                _emit_complex_brack(brack, out, indent=indent)
                prev_type = TT.RBRACK
                prev_text = "]"
            else:
                inline = _render_tokens_inline(brack)
                if started:
                    out.write(" ")
                out.write(inline)
                prev_type = TT.RBRACK
                prev_text = "]"
            started = True
            i = end
            continue
        if tok.type == TT.LPAREN:
            # Decide function-call vs. parenthesised-expression vs.
            # collection/action-payload wrapper.
            #
            # Function calls (`COUNT(?x)`) are tight against the preceding
            # identifier. Operator-keywords (`IN`, `NOT IN`) and action
            # invocations (`pred ( [ payload ] )`) get a leading space.
            # An action invocation is recognised by the wrapper containing
            # a complex bracketed payload — that pattern reads as
            # "predicate applied to a structured object", not as a function
            # call, and so deserves the breathing room a function call
            # would not.
            paren, end = _grab_balanced(tokens, i, TT.LPAREN, TT.RPAREN)
            operator_keywords = {"IN", "NOT"}
            is_call = (
                prev_type
                in (
                    TT.PNAME,
                    TT.KEYWORD,
                    TT.IDENT,
                    TT.RPAREN,
                )
                and prev_text not in operator_keywords
            )
            if _is_complex_bracket(paren):
                # Action-payload / collection wrapper. ALWAYS space-before
                # so the predicate stays readable.
                if started:
                    out.write(" ")
                out.write("(")
                _emit_collection_multiline(paren, out, indent=indent)
                out.write(")")
                prev_type = TT.RPAREN
                prev_text = ")"
                started = True
                i = end
                continue
            # Simple inline parens (function call args, expression group).
            paren_depth += 1
            if not started or is_call:
                out.write("(")
            else:
                out.write(" (")
            started = True
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        if tok.type == TT.RPAREN:
            paren_depth -= 1
            out.write(")")
            started = True
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        if tok.type == TT.PATH_OP:
            out.write(tok.text)
            started = True
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        if tok.type == TT.COMMA:
            out.write(",")
            if i + 1 < len(tokens) and tokens[i + 1].type not in (TT.RPAREN, TT.COMMA):
                out.write(" ")
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        # Default token. Leading space unless tight-binding context.
        if started and prev_type not in (TT.LPAREN, TT.PATH_OP):
            out.write(" ")
        out.write(tok.text)
        started = True
        prev_type = tok.type
        prev_text = tok.text
        i += 1
    return i


def _emit_complex_brack(brack: list[Token], out: _Emitter, *, indent: int) -> None:
    """Render a complex `[ ... ]` blank node multi-line:

    ::

        [
            pred obj ;
            pred obj-with-nested ( ... ) ;
            ...
        ]

    Each predicate-object pair is indented one level deeper than
    ``indent``. Pairs whose object contains a complex nested ``()`` or
    ``[]`` recurse into the multi-line emitter; trivial pairs are
    rendered inline."""
    out.write("[")
    out.newline()
    inner_indent = indent + 1
    body = brack[1:-1]
    pairs = _split_at_top_level(body, TT.SEMI)
    for idx, pair in enumerate(pairs):
        out.write(INDENT * inner_indent)
        _emit_pair(pair, out, indent=inner_indent)
        if idx < len(pairs) - 1:
            out.write(" ;")
        out.newline()
    out.write(INDENT * indent + "]")


def _emit_pair(pair: list[Token], out: _Emitter, *, indent: int) -> None:
    """Emit one `predicate object[, object]*` pair. Walks token-by-token
    using the same spacing rules as ``_emit_triple``; recurses into
    multi-line layout when it hits a complex nested ``[]`` or ``()``."""
    prev_type: TT | None = None
    prev_text: str = ""
    operator_keywords = {"IN", "NOT"}
    j = 0
    while j < len(pair):
        tok = pair[j]
        if tok.type == TT.LBRACK:
            sub, end = _grab_balanced(pair, j, TT.LBRACK, TT.RBRACK)
            if _is_complex_bracket(sub):
                if prev_type is not None:
                    out.write(" ")
                _emit_complex_brack(sub, out, indent=indent)
            else:
                if prev_type is not None:
                    out.write(" ")
                out.write(_render_tokens_inline(sub))
            prev_type = TT.RBRACK
            prev_text = "]"
            j = end
            continue
        if tok.type == TT.LPAREN:
            sub, end = _grab_balanced(pair, j, TT.LPAREN, TT.RPAREN)
            is_call = (
                prev_type
                in (
                    TT.PNAME,
                    TT.KEYWORD,
                    TT.IDENT,
                    TT.RPAREN,
                )
                and prev_text not in operator_keywords
            )
            if _is_complex_bracket(sub):
                # Action-payload / collection wrapper inside a pair.
                if prev_type is not None:
                    out.write(" ")
                out.write("(")
                _emit_collection_multiline(sub, out, indent=indent)
                out.write(")")
                prev_type = TT.RPAREN
                prev_text = ")"
                j = end
                continue
            # Simple inline paren.
            if prev_type is None or is_call:
                out.write("(")
            else:
                out.write(" (")
            # Walk through the inline content.
            j += 1
            inner_prev_type: TT | None = TT.LPAREN
            while j < len(pair) and pair[j].type != TT.RPAREN:
                t = pair[j]
                if t.type == TT.COMMA:
                    out.write(",")
                    if j + 1 < len(pair) and pair[j + 1].type != TT.RPAREN:
                        out.write(" ")
                    inner_prev_type = TT.COMMA
                    j += 1
                    continue
                if t.type == TT.PATH_OP:
                    out.write(t.text)
                    inner_prev_type = TT.PATH_OP
                    j += 1
                    continue
                if inner_prev_type is not None and inner_prev_type not in (
                    TT.LPAREN,
                    TT.PATH_OP,
                ):
                    out.write(" ")
                out.write(t.text)
                inner_prev_type = t.type
                j += 1
            out.write(")")
            prev_type = TT.RPAREN
            prev_text = ")"
            j += 1  # past the RPAREN
            continue
        if tok.type == TT.PATH_OP:
            out.write(tok.text)
            prev_type = tok.type
            prev_text = tok.text
            j += 1
            continue
        if tok.type == TT.COMMA:
            out.write(",")
            if j + 1 < len(pair):
                out.write(" ")
            prev_type = tok.type
            prev_text = tok.text
            j += 1
            continue
        # Default token.
        if prev_type is not None and prev_type not in (TT.LPAREN, TT.PATH_OP):
            out.write(" ")
        out.write(tok.text)
        prev_type = tok.type
        prev_text = tok.text
        j += 1


def _split_at_top_level(tokens: list[Token], split_t: TT) -> list[list[Token]]:
    """Split ``tokens`` by ``split_t`` occurrences at depth 0. ``depth``
    is incremented by LBRACK / LPAREN / LBRACE."""
    out: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.type in (TT.LBRACK, TT.LPAREN, TT.LBRACE):
            depth += 1
        elif tok.type in (TT.RBRACK, TT.RPAREN, TT.RBRACE):
            depth -= 1
        if tok.type == split_t and depth == 0:
            out.append(current)
            current = []
            continue
        current.append(tok)
    if current:
        out.append(current)
    return out


def _emit_collection_multiline(
    paren: list[Token], out: _Emitter, *, indent: int
) -> None:
    """Render the interior of a `( ... )` that wraps complex content. Two
    layouts depending on member count:

    * **Single complex member** (typical: `( [ ... ] )` wrapping one
      complex blank node): the `(` and `)` hug the member's first/last
      line. Caller has written the open `(`; we emit the member directly
      and leave the cursor positioned for the caller to write `)`.
    * **Multiple members** (typical: `( [ ... ] [ ... ] )` list): each
      member on its own line at `indent + 1`; `)` at `indent` (caller
      writes the `)`; we leave the cursor at end-of-line for it).
    """
    body = paren[1:-1]
    # Discover top-level member boundaries. Members in a SPARQL collection
    # are separated by whitespace (no explicit token) — we identify them
    # by being well-formed bracketed groups or single terms at depth 0.
    members = _split_collection_members(body)
    if len(members) == 1:
        # Single complex member — emit it tight to the parens.
        member = members[0]
        if member and member[0].type == TT.LBRACK:
            brack, _ = _grab_balanced(member, 0, TT.LBRACK, TT.RBRACK)
            _emit_complex_brack(brack, out, indent=indent)
        else:
            out.write(_render_tokens_inline(member))
        return
    # Multiple members — one per line, each rendered INLINE. The
    # collection itself is the multi-line carrier; members read better
    # when each `[ ... ]` stays on one line even if its property-list
    # is long. (Multi-line members inside a multi-member collection
    # produce a noisy nested-block effect.)
    out.newline()
    inner_indent = indent + 1
    for member in members:
        out.write(INDENT * inner_indent)
        out.write(_render_tokens_inline(member))
        out.newline()
    out.write(INDENT * indent)


def _split_collection_members(body: list[Token]) -> list[list[Token]]:
    """Split a `(...)` collection's interior into its member terms. A
    member is either a single token (PNAME/IRI/VARIABLE/STRING/NUMBER/
    BOOL/BLANK) or a balanced ``[...]`` group."""
    members: list[list[Token]] = []
    i = 0
    while i < len(body):
        tok = body[i]
        if tok.type == TT.LBRACK:
            sub, end = _grab_balanced(body, i, TT.LBRACK, TT.RBRACK)
            members.append(sub)
            i = end
            continue
        if tok.type == TT.LPAREN:
            sub, end = _grab_balanced(body, i, TT.LPAREN, TT.RPAREN)
            members.append(sub)
            i = end
            continue
        if tok.type in (
            TT.PNAME,
            TT.IRI,
            TT.VARIABLE,
            TT.STRING,
            TT.NUMBER,
            TT.BOOL,
            TT.BLANK,
        ):
            members.append([tok])
        i += 1
    return members


def _emit_non_triple(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    pad = INDENT * indent
    keyword = tokens[i].text
    if keyword in {"OPTIONAL", "MINUS", "SERVICE"}:
        out.write(pad + keyword + " ")
        i += 1
        if (
            i < len(tokens)
            and tokens[i].type == TT.KEYWORD
            and tokens[i].text == "SILENT"
        ):
            out.write("SILENT ")
            i += 1
        if i < len(tokens) and tokens[i].type in (TT.IRI, TT.VARIABLE, TT.PNAME):
            out.write(tokens[i].text + " ")
            i += 1
        return _emit_group(tokens, i, out, indent=indent)
    if keyword == "UNION":
        out.write(pad + "UNION ")
        i += 1
        return _emit_group(tokens, i, out, indent=indent)
    if keyword == "FILTER":
        out.write(pad + "FILTER ")
        i += 1
        if (
            i < len(tokens)
            and tokens[i].type == TT.KEYWORD
            and tokens[i].text in {"EXISTS", "NOT"}
        ):
            return _emit_filter_exists(tokens, i, out, indent=indent)
        return _emit_paren_expression(tokens, i, out, indent=indent)
    if keyword == "BIND":
        out.write(pad + "BIND")
        i += 1
        return _emit_paren_expression(tokens, i, out, indent=indent)
    if keyword == "VALUES":
        out.write(pad + "VALUES ")
        i += 1
        return _emit_values(tokens, i, out, indent=indent)
    out.line(pad + keyword)
    return i + 1


def _emit_filter_exists(
    tokens: list[Token], i: int, out: _Emitter, *, indent: int
) -> int:
    if tokens[i].text == "NOT":
        out.write("NOT ")
        i += 1
    if i < len(tokens) and tokens[i].text == "EXISTS":
        out.write("EXISTS ")
        i += 1
    return _emit_group(tokens, i, out, indent=indent)


def _should_multiline_paren_list(paren: list[Token], *, current_col: int) -> bool:
    """A parenthesised paren-list (``(a, b, c)``) deserves multi-line
    layout when it both (a) contains commas at depth 0 of the paren
    (i.e. is actually a list, not just a parenthesised expression) and
    (b) its inline render appended to the current cursor position would
    push past ``INLINE_FOLD_MAX``."""
    body = paren[1:-1]
    depth = 0
    has_comma = False
    for tok in body:
        if tok.type in (TT.LPAREN, TT.LBRACK, TT.LBRACE):
            depth += 1
        elif tok.type in (TT.RPAREN, TT.RBRACK, TT.RBRACE):
            depth -= 1
        elif tok.type == TT.COMMA and depth == 0:
            has_comma = True
    if not has_comma:
        return False
    inline = _render_tokens_inline(paren)
    return current_col + len(inline) > INLINE_FOLD_MAX


def _emit_paren_list_multiline(
    paren: list[Token], out: _Emitter, *, indent: int
) -> None:
    """Emit a parenthesised comma-list as a multi-line block:

    ::

        (
            value1,
            value2,
            ...
        )

    The opening ``(`` is written by the caller on the current line; we
    write the newline + indented values + closing ``)`` at ``indent``.
    Each value is the chunk of tokens between commas at depth 0; values
    can themselves contain nested parens/brackets, which are rendered
    inline via ``_render_tokens_inline``.
    """
    out.write("(")
    out.newline()
    inner_indent = indent + 1
    body = paren[1:-1]
    values = _split_at_top_level(body, TT.COMMA)
    for idx, value in enumerate(values):
        out.write(INDENT * inner_indent + _render_tokens_inline(value))
        if idx < len(values) - 1:
            out.write(",")
        out.newline()
    out.write(INDENT * indent + ")")


def _emit_paren_expression(
    tokens: list[Token], i: int, out: _Emitter, *, indent: int
) -> int:
    """Emit one balanced parenthesised expression on the current line,
    applying the same SPARQL spacing rules as ``_emit_triple``:

    * ``(`` is tight against the preceding identifier when it's a
      function call; loose (space-before) for operator-keywords like
      ``IN``.
    * ``)`` always tight on its left.
    * ``,`` flush against its left token, single space after.
    * Property-path operators bind tightly.

    When a *nested* ``(`` opens a comma-list whose inline render would
    push the current line past ``INLINE_FOLD_MAX`` columns (typically
    ``FILTER (?p IN (long, list, of, items))``), the list is broken
    multi-line:

    ::

        FILTER (?p IN (
            item1,
            item2,
            ...
        ))
    """
    if i >= len(tokens) or tokens[i].type != TT.LPAREN:
        out.newline()
        return i
    depth = 0
    prev_type: TT | None = None
    prev_text: str = ""
    operator_keywords = {"IN", "NOT"}
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.LPAREN:
            depth += 1
            tight = (
                prev_type in (TT.PNAME, TT.KEYWORD, TT.IDENT, TT.RPAREN)
                and prev_text not in operator_keywords
            )
            # Nested LPAREN (depth > 1 after the increment) — check if the
            # contained content is a long comma-list that should multi-line.
            # `_emit_paren_list_multiline` writes both the opening `(` and
            # the closing `)` itself; the caller only writes the leading
            # space (or omits it for tight-binding contexts).
            if depth > 1:
                nested, nested_end = _grab_balanced(tokens, i, TT.LPAREN, TT.RPAREN)
                if _should_multiline_paren_list(
                    nested, current_col=out.current_line_len()
                ):
                    if prev_type is not None and not tight:
                        out.write(" ")
                    _emit_paren_list_multiline(nested, out, indent=indent)
                    prev_type = TT.RPAREN
                    prev_text = ")"
                    depth -= 1
                    i = nested_end
                    continue
            if prev_type is None or tight:
                out.write("(")
            else:
                out.write(" (")
            prev_type = tok.type
            prev_text = "("
            i += 1
            continue
        if tok.type == TT.RPAREN:
            depth -= 1
            out.write(")")
            prev_type = tok.type
            prev_text = ")"
            i += 1
            if depth == 0:
                out.newline()
                return i
            continue
        if tok.type == TT.COMMA:
            out.write(",")
            # Trailing space only when more content follows.
            if i + 1 < len(tokens) and tokens[i + 1].type not in (TT.RPAREN, TT.COMMA):
                out.write(" ")
            prev_type = tok.type
            prev_text = ","
            i += 1
            continue
        if tok.type == TT.PATH_OP:
            out.write(tok.text)
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        if tok.type == TT.COMMENT:
            out.write(f" {tok.text}")
            prev_type = tok.type
            prev_text = tok.text
            i += 1
            continue
        # Default token. Leading space unless previous was LPAREN, COMMA
        # (which already added its own space), or PATH_OP.
        if prev_type is not None and prev_type not in (TT.LPAREN, TT.COMMA, TT.PATH_OP):
            out.write(" ")
        out.write(tok.text)
        prev_type = tok.type
        prev_text = tok.text
        i += 1
    out.newline()
    return i


def _emit_values(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    """VALUES blocks — kept inline. Refinement deferred (uncommon in practice)."""
    start = i
    depth = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.LBRACE:
            depth += 1
        elif tok.type == TT.RBRACE:
            depth -= 1
            i += 1
            if depth == 0:
                break
        i += 1
    out.write(" ".join(t.text for t in tokens[start:i]))
    out.newline()
    return i


def _emit_graph(tokens: list[Token], i: int, out: _Emitter, *, indent: int) -> int:
    pad = INDENT * indent
    out.write(pad + "GRAPH ")
    i += 1
    if i < len(tokens) and tokens[i].type in (TT.IRI, TT.VARIABLE, TT.PNAME):
        out.write(tokens[i].text + " ")
        i += 1
    return _emit_group(tokens, i, out, indent=indent)
