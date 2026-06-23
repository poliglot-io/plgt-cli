"""Opinionated Turtle formatter — canonical output for canonical input.

Layout rules (frozen — there is no config):

* **Indent**: 4 spaces. Each blank-node group ``[ ... ]`` adds one level.
* **@prefix / @base block**: at the top, alphabetised by prefix label (the
  default-empty prefix sorts first), exactly two blank lines after the
  block before the first subject or top-level comment.
* **Subject blocks**: one named subject per block, exactly one blank line
  between blocks.
* **Predicate list**: one predicate per line, joined with `` ;`` at end of
  line. ``a`` (rdf:type) sorts first if present; remaining predicates keep
  the author's order.
* **Multi-value lists** (``,`` separator): inline if every value is a
  simple term and the joined line fits; otherwise one per line.
* **Blank-node objects** (``[ ... ]``): open bracket on the predicate's
  line, body indented one level, close bracket at the predicate's indent.
* **String literals**: passed through verbatim. Never reflowed.
* **Comments**: preserved at the token position the author placed them.
* **Trailing newline**: exactly one.

Idempotent: ``format_turtle(format_turtle(x)) == format_turtle(x)``.
"""

from __future__ import annotations

from plgt.services.formatter.lexer import TT, Token, tokenize

INDENT = "    "
MAX_INLINE_COL = 100

# Predicate URIs whose object values get canonicalised. Currently just
# ``plgt-mtx:imports`` — imports identify external namespaces and the
# full IRI is the canonical identity (different matrices can use different
# prefix labels for the same namespace). The formatter rewrites every
# value of these predicates to its full ``<...>`` form regardless of
# whether a matching prefix is defined in the local @prefix table.
EXPAND_TO_FULL_IRI_PREDICATES = frozenset(
    {"https://poliglot.io/os/spec/matrix#imports"}
)


def format_turtle(source: str) -> str:
    """Reformat ``source`` per the rules above. On lexer error returns the
    input unchanged — a formatter must never destroy un-parseable input."""
    try:
        tokens = tokenize(source, sparql_mode=False)
    except ValueError:
        return source

    out = _Emitter()
    i = 0

    directives, i = _collect_directives(tokens, i)
    _emit_directives(out, directives)
    prefix_table = _build_prefix_table(directives)

    if directives and i < len(tokens):
        out.blank_lines(2)

    first_stmt = True
    previous_was_comment = False
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == TT.COMMENT:
            # Comments are author intent — the formatter doesn't transform
            # the spacing around them at all. Whatever blank-line count the
            # author wrote leading into this comment line, we emit. This
            # preserves the full range of intentional layouts: tight section
            # banners, multi-line prose paragraphs, AND deliberately
            # spaced-apart comment blocks.
            if first_stmt:
                pass  # 2-blank post-directive gap already emitted
            elif tok.leading_blank_lines > 0:
                out.blank_lines(tok.leading_blank_lines)
            out.line(tok.text)
            i += 1
            first_stmt = False
            previous_was_comment = True
            continue
        # Real statement.
        if first_stmt:
            pass
        elif previous_was_comment:
            # Coming out of a comment region — preserve the author's spacing
            # so a tight comment-then-statement layout stays tight, and a
            # deliberately-spaced comment-then-statement stays spaced.
            if tok.leading_blank_lines > 0:
                out.blank_lines(tok.leading_blank_lines)
        else:
            # Statement → statement: formatter's standard one-blank rule.
            out.blank_lines(1)
        i = _emit_statement(tokens, i, out, indent=0, prefix_table=prefix_table)
        first_stmt = False
        previous_was_comment = False

    return out.finish()


def _build_prefix_table(
    directives: list[tuple[Token, list[Token]]],
) -> dict[str, str]:
    """Return ``{prefix_label: namespace_iri}`` for every ``@prefix`` directive.

    Used to (a) resolve predicate prefixed-names so we can recognise
    ``plgt-mtx:imports`` regardless of which label the matrix uses for the
    matrix-namespace prefix, and (b) expand object prefixed-names to their
    full IRI form for canonicalisation-target predicates.
    """
    table: dict[str, str] = {}
    for tok, _ in directives:
        if tok.type != TT.PREFIX_DECL:
            continue
        # tok.text is the full directive: `@prefix label: <iri> .`
        # Parse out the label (after `@prefix`) and the IRI (between `<>`).
        text = tok.text
        # Strip leading `@prefix`.
        rest = text.split(None, 1)[1] if " " in text else text
        colon = rest.find(":")
        if colon == -1:
            continue
        label = rest[:colon].strip()
        # The IRI is between the next `<` and `>`.
        lt = rest.find("<", colon)
        gt = rest.find(">", lt)
        if lt == -1 or gt == -1:
            continue
        table[label] = rest[lt + 1 : gt]
    return table


def _resolve_pname(text: str, prefix_table: dict[str, str]) -> str | None:
    """Resolve a prefixed name like ``foo:bar`` or ``foo:`` to a full IRI
    using ``prefix_table``. Returns ``None`` when the prefix label isn't
    bound. The default-empty prefix form (``:bar``) is handled by looking
    up the empty-string key.
    """
    if ":" not in text:
        return None
    label, local = text.split(":", 1)
    namespace = prefix_table.get(label)
    if namespace is None:
        return None
    return namespace + local


def _resolve_predicate_iri(
    predicate_token: Token, prefix_table: dict[str, str]
) -> str | None:
    """Return the full IRI for ``predicate_token`` (PNAME or IRI), or
    ``None`` when the predicate is the bare ``a`` keyword (which we don't
    expand) or the prefix can't be resolved.
    """
    if predicate_token.type == TT.IRI:
        return predicate_token.text[1:-1]  # strip ``<>``
    if predicate_token.type == TT.PNAME and predicate_token.text != "a":
        return _resolve_pname(predicate_token.text, prefix_table)
    return None


# ---------------------------------------------------------------------------
# Emitter.


class _Emitter:
    """Line buffer with strict blank-line control."""

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
        """Ensure exactly ``n`` blank lines separate the previous content
        from whatever's emitted next."""
        if self._current:
            self.newline()
        while self._lines and self._lines[-1] == "":
            self._lines.pop()
        for _ in range(n):
            self._lines.append("")

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
    """Return [(directive_token, attached_comments)] in source order, plus
    the cursor position after the last directive. Comments interleaved
    with directives stay attached to the next directive. Comments AFTER
    the last directive (e.g. introducing the first statement) are left in
    the token stream for the main loop to emit."""
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
    # Any comments collected after the last directive but with no further
    # directive following must be returned to the caller — they belong to
    # the first statement, not to the directive block.
    if leading:
        i = leading_start
    return directives, i


def _emit_directives(
    out: _Emitter, directives: list[tuple[Token, list[Token]]]
) -> None:
    """Emit @base first, then @prefix alphabetised. Comments attached to
    a directive line are preserved on that line."""
    base = [d for d in directives if d[0].type == TT.BASE_DECL]
    prefixes = [d for d in directives if d[0].type == TT.PREFIX_DECL]
    prefixes.sort(key=lambda d: _prefix_label(d[0].text))
    for tok, comments in base + prefixes:
        leading_cmt = [c for c in comments if c.line != tok.line]
        same_line = [c for c in comments if c.line == tok.line]
        for c in leading_cmt:
            out.line(c.text)
        body = _canonicalise_directive(tok.text)
        if same_line:
            out.line(f"{body}  {same_line[0].text}")
        else:
            out.line(body)


def _canonicalise_directive(text: str) -> str:
    return " ".join(text.split())


def _prefix_label(text: str) -> str:
    pieces = text.split(None, 1)
    if len(pieces) < 2:
        return ""
    rest = pieces[1]
    colon = rest.find(":")
    if colon == -1:
        return ""
    return rest[:colon].strip()


# ---------------------------------------------------------------------------
# Statements.


def _emit_statement(
    tokens: list[Token],
    i: int,
    out: _Emitter,
    *,
    indent: int,
    prefix_table: dict[str, str] | None = None,
) -> int:
    """Emit one Turtle statement: SUBJECT predicate-list DOT. Returns the
    index after the terminating DOT.

    ``prefix_table`` is the matrix's ``@prefix`` bindings, threaded through
    so per-predicate expansion rules (e.g. ``plgt-mtx:imports`` → full
    IRI) can resolve predicate and object PNAMEs to canonical form.
    """
    pad = INDENT * indent
    # Subject on its own line.
    i, subject_text = _render_term_inline(tokens, i, indent=indent)
    out.line(pad + subject_text)
    # Predicate list (each on its own line, joined with `;`).
    i = _emit_predicate_list(
        tokens, i, out, indent=indent + 1, prefix_table=prefix_table
    )
    # Closing dot.
    if i < len(tokens) and tokens[i].type == TT.DOT:
        out.write(" .")
        i += 1
        if (
            i < len(tokens)
            and tokens[i].type == TT.COMMENT
            and tokens[i].line == tokens[i - 1].line
        ):
            out.write(f"  {tokens[i].text}")
            i += 1
        out.newline()
    return i


def _emit_predicate_list(
    tokens: list[Token],
    i: int,
    out: _Emitter,
    *,
    indent: int,
    prefix_table: dict[str, str] | None = None,
) -> int:
    """Walk to end-of-statement (DOT or matching RBRACK), splitting on
    SEMI at depth 0. Emit each predicate-object group on its own line,
    joined with `` ;`` ; ``a`` (rdf:type) moved to the head of the list.
    Comments at depth 0 are emitted between groups as standalone lines so
    they don't get absorbed into a predicate-object group (which would
    turn the next predicate into a comment continuation).

    Returns the index of the terminating DOT/RBRACK (caller writes it)."""
    segments: list[Token | list[Token]] = []
    current: list[Token] = []
    depth = 0
    while i < len(tokens):
        tok = tokens[i]
        if depth == 0 and tok.type in (TT.DOT, TT.RBRACK):
            break
        if tok.type == TT.SEMI and depth == 0:
            if current:
                segments.append(current)
                current = []
            i += 1
            continue
        if tok.type == TT.COMMENT and depth == 0:
            if current:
                segments.append(current)
                current = []
            segments.append(tok)
            i += 1
            continue
        if tok.type in (TT.LBRACK, TT.LPAREN):
            depth += 1
        elif tok.type in (TT.RBRACK, TT.RPAREN):
            depth -= 1
        current.append(tok)
        i += 1
    if current:
        segments.append(current)

    # Reorder groups so `a` (rdf:type) comes first; comments stay where
    # they were authored (they label the predicate they precede).
    def is_type_group(seg: Token | list[Token]) -> bool:
        return (
            isinstance(seg, list)
            and bool(seg)
            and seg[0].type == TT.PNAME
            and seg[0].text == "a"
        )

    type_segs = [s for s in segments if is_type_group(s)]
    other_segs = [s for s in segments if not is_type_group(s)]
    ordered = type_segs + other_segs

    pad = INDENT * indent
    # Track whether the last emitted segment was a group (needs trailing
    # ` ;` before the next group/comment) or a comment (no trailing ` ;`).
    last_kind: str | None = None  # "group" | "comment" | None
    for seg in ordered:
        if isinstance(seg, Token):
            # Standalone comment — terminate the previous group with ` ;`.
            if last_kind == "group":
                out.write(" ;")
                out.newline()
            out.line(pad + seg.text)
            last_kind = "comment"
        else:
            if last_kind == "group":
                out.write(" ;")
                out.newline()
            _emit_predicate_object_group(
                seg, out, indent=indent, prefix_table=prefix_table
            )
            last_kind = "group"
    return i


def _emit_predicate_object_group(
    group: list[Token],
    out: _Emitter,
    *,
    indent: int,
    prefix_table: dict[str, str] | None = None,
) -> None:
    """One predicate followed by one or more comma-separated objects.

    Layout: ``<indent>predicate object`` on one line if there's a single
    simple object; otherwise ``<indent>predicate obj1,\\n<indent+1>obj2,\\n...``.

    When the predicate resolves to a canonicalisation target (currently
    just ``plgt-mtx:imports``), every PNAME object is rewritten to its
    full ``<...>`` IRI before layout. The namespace lookup uses
    ``prefix_table`` (the matrix's ``@prefix`` bindings).
    """
    pad = INDENT * indent
    predicate = group[0]
    out.write(pad + predicate.text)

    # Split remainder by COMMA at depth 0.
    objects: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in group[1:]:
        if tok.type == TT.COMMA and depth == 0:
            if current:
                objects.append(current)
                current = []
            continue
        if tok.type in (TT.LBRACK, TT.LPAREN):
            depth += 1
        elif tok.type in (TT.RBRACK, TT.RPAREN):
            depth -= 1
        current.append(tok)
    if current:
        objects.append(current)

    # Apply per-predicate object canonicalisation. For the imports
    # predicate (and any future entry in EXPAND_TO_FULL_IRI_PREDICATES),
    # every single-token PNAME object becomes a full IRI.
    if prefix_table is not None:
        predicate_iri = _resolve_predicate_iri(predicate, prefix_table)
        if predicate_iri in EXPAND_TO_FULL_IRI_PREDICATES:
            objects = [_expand_pname_object(obj, prefix_table) for obj in objects]

    # Defensive: a well-formed predicate-object group always has at least
    # one object. An empty list can only arise from a malformed grouping
    # (e.g. a stray comment token treated as a predicate); emit just the
    # predicate text rather than indexing ``objects[0]`` and crashing.
    if not objects:
        return

    # Single object: render inline (handles blank-node objects too —
    # _render_complex_object_inline knows how to multi-line a [ ... ]).
    if len(objects) == 1:
        out.write(" ")
        _render_object(objects[0], out, indent=indent)
        return

    # Multi-object: try inline-joined first.
    if all(_is_simple_object(o) for o in objects):
        joined = ", ".join(_simple_object_text(o) for o in objects)
        candidate_len = len(pad) + len(predicate.text) + 1 + len(joined)
        if candidate_len <= MAX_INLINE_COL:
            out.write(" " + joined)
            return

    # Fall back: one object per line, continuation values aligned at the
    # SAME indent as the predicate (not indent+1) — matches conventional
    # Turtle pretty-printing.
    out.write(" ")
    _render_object(objects[0], out, indent=indent)
    for obj in objects[1:]:
        out.write(",")
        out.newline()
        out.write(INDENT * indent)
        _render_object(obj, out, indent=indent)


def _expand_pname_object(obj: list[Token], prefix_table: dict[str, str]) -> list[Token]:
    """If ``obj`` is a single-token PNAME and the prefix resolves against
    ``prefix_table``, return a new single-token IRI list with the full
    ``<...>`` form. Otherwise return ``obj`` unchanged.

    Used by canonicalisation-target predicates (e.g. ``plgt-mtx:imports``)
    so authors who write a value as ``dprod:`` get the canonical
    ``<https://ekgf.github.io/dprod/>`` form on save."""
    if len(obj) != 1 or obj[0].type != TT.PNAME:
        return obj
    iri = _resolve_pname(obj[0].text, prefix_table)
    if iri is None:
        return obj
    return [
        Token(
            TT.IRI,
            f"<{iri}>",
            obj[0].line,
            obj[0].col,
            obj[0].leading_blank_lines,
        )
    ]


def _is_simple_object(obj: list[Token]) -> bool:
    """Object that fits on one line: no blank-node group, no nested
    collection, no inline comments."""
    return all(t.type not in (TT.LBRACK, TT.LPAREN, TT.COMMENT) for t in obj)


def _simple_object_text(obj: list[Token]) -> str:
    return " ".join(t.text for t in obj)


def _render_object(obj: list[Token], out: _Emitter, *, indent: int) -> None:
    """Emit a single object's tokens. Handles inline simple objects and
    nested blank-node groups / collections that need multi-line layout.

    Caller has already positioned the cursor at the column the object
    should start in (i.e. no leading space written here)."""
    if _is_simple_object(obj):
        out.write(_simple_object_text(obj))
        return
    # Find the (rare) case where the object IS a single bracketed term.
    if obj[0].type == TT.LBRACK and _is_balanced_bracket(obj, TT.LBRACK, TT.RBRACK):
        _emit_blank_node_object(obj, out, indent=indent)
        return
    if obj[0].type == TT.LPAREN and _is_balanced_bracket(obj, TT.LPAREN, TT.RPAREN):
        _emit_collection_object(obj, out, indent=indent)
        return
    # Mixed / unusual — fall back to a single-spaced join.
    out.write(" ".join(t.text for t in obj))


def _is_balanced_bracket(obj: list[Token], open_t: TT, close_t: TT) -> bool:
    if not obj or obj[0].type != open_t or obj[-1].type != close_t:
        return False
    depth = 0
    for t in obj:
        if t.type == open_t:
            depth += 1
        elif t.type == close_t:
            depth -= 1
            if depth == 0 and t is not obj[-1]:
                return False
    return depth == 0


def _emit_blank_node_object(obj: list[Token], out: _Emitter, *, indent: int) -> None:
    """Render ``[ ... ]`` as a multi-line block: ``[`` on the current
    line, predicate list indented, ``]`` at the current term's indent."""
    inner = obj[1:-1]
    # Empty group: render as `[]`.
    non_comment = [t for t in inner if t.type != TT.COMMENT]
    if not non_comment:
        out.write("[]")
        return
    out.write("[")
    out.newline()
    _emit_predicate_list_from_tokens(inner, out, indent=indent + 1)
    out.write(INDENT * indent + "]")


def _emit_collection_object(obj: list[Token], out: _Emitter, *, indent: int) -> None:
    """Render ``( a b c )``. Inline if every member is simple and the
    joined form fits MAX_INLINE_COL; otherwise multi-line."""
    inner = obj[1:-1]
    simple_members = all(
        t.type not in (TT.LBRACK, TT.LPAREN, TT.COMMENT) for t in inner
    )
    if simple_members:
        joined = " ".join(t.text for t in inner)
        candidate = f"( {joined} )"
        # Rough column estimate: assume current write position is near
        # the predicate object column; we don't track exact column.
        # Use a conservative threshold.
        if len(candidate) <= MAX_INLINE_COL:
            out.write(candidate)
            return
    out.write("(")
    out.newline()
    j = 0
    while j < len(inner):
        tok = inner[j]
        if tok.type == TT.COMMENT:
            out.line(INDENT * (indent + 1) + tok.text)
            j += 1
            continue
        # Each member is one token (for simple cases) or a balanced bracket.
        if tok.type == TT.LBRACK:
            sub = _grab_balanced(inner, j, TT.LBRACK, TT.RBRACK)
            out.write(INDENT * (indent + 1))
            _emit_blank_node_object(sub, out, indent=indent + 1)
            out.newline()
            j += len(sub)
        elif tok.type == TT.LPAREN:
            sub = _grab_balanced(inner, j, TT.LPAREN, TT.RPAREN)
            out.write(INDENT * (indent + 1))
            _emit_collection_object(sub, out, indent=indent + 1)
            out.newline()
            j += len(sub)
        else:
            out.line(INDENT * (indent + 1) + tok.text)
            j += 1
    out.write(INDENT * indent + ")")


def _grab_balanced(tokens: list[Token], i: int, open_t: TT, close_t: TT) -> list[Token]:
    """Return the slice from ``i`` through (and including) the matching
    close bracket."""
    depth = 0
    j = i
    while j < len(tokens):
        if tokens[j].type == open_t:
            depth += 1
        elif tokens[j].type == close_t:
            depth -= 1
            if depth == 0:
                return tokens[i : j + 1]
        j += 1
    return tokens[i:]


def _emit_predicate_list_from_tokens(
    tokens: list[Token], out: _Emitter, *, indent: int
) -> None:
    """Like _emit_predicate_list, but operates on a pre-sliced token list
    (the body of a blank-node group). Caller has positioned the cursor at
    the start of a new line. Emits each predicate-object group on its own
    line with `;`-separation; no trailing dot.

    Comments at depth 0 are pulled out as standalone segments (exactly as
    the top-level ``_emit_predicate_list`` does), so a comment sitting
    inside a nested blank-node predicate list is emitted on its own line
    rather than being folded into a predicate-object group — which would
    otherwise leave that group with an empty object list and either glue
    the following predicate onto the comment line or crash the emitter."""
    segments: list[Token | list[Token]] = []
    current: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.type == TT.SEMI and depth == 0:
            if current:
                segments.append(current)
                current = []
            continue
        if tok.type == TT.COMMENT and depth == 0:
            if current:
                segments.append(current)
                current = []
            segments.append(tok)
            continue
        if tok.type in (TT.LBRACK, TT.LPAREN):
            depth += 1
        elif tok.type in (TT.RBRACK, TT.RPAREN):
            depth -= 1
        current.append(tok)
    if current:
        segments.append(current)

    def is_type_group(seg: Token | list[Token]) -> bool:
        return (
            isinstance(seg, list)
            and bool(seg)
            and seg[0].type == TT.PNAME
            and seg[0].text == "a"
        )

    type_segs = [s for s in segments if is_type_group(s)]
    other_segs = [s for s in segments if not is_type_group(s)]
    ordered = type_segs + other_segs

    # Track the last emitted segment kind so a predicate-object group gets a
    # trailing ` ;` only when another group/comment follows, and standalone
    # comment lines never receive one.
    last_kind: str | None = None  # "group" | "comment" | None
    for seg in ordered:
        if isinstance(seg, Token):
            if last_kind == "group":
                out.write(" ;")
                out.newline()
            out.line(INDENT * indent + seg.text)
            last_kind = "comment"
        else:
            if last_kind == "group":
                out.write(" ;")
                out.newline()
            _emit_predicate_object_group(seg, out, indent=indent)
            last_kind = "group"
    out.newline()


def _render_term_inline(tokens: list[Token], i: int, *, indent: int) -> tuple[int, str]:
    """Render a subject term (IRI/PNAME/BLANK or, rarely, a bracketed
    term) as a single-line string. Returns (new_i, rendered_text)."""
    tok = tokens[i]
    if tok.type in (TT.IRI, TT.PNAME, TT.BLANK):
        return i + 1, tok.text
    if tok.type == TT.LBRACK:
        sub = _grab_balanced(tokens, i, TT.LBRACK, TT.RBRACK)
        return i + len(sub), " ".join(t.text for t in sub)
    if tok.type == TT.LPAREN:
        sub = _grab_balanced(tokens, i, TT.LPAREN, TT.RPAREN)
        return i + len(sub), " ".join(t.text for t in sub)
    return i + 1, tok.text
