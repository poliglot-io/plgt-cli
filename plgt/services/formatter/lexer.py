"""Lexer shared by the Turtle and SPARQL formatters.

The formatter is intentionally NOT a parser. We tokenise far enough to know
where statements end (``.``), where same-subject continuations happen
(``;``), where multi-value lists are (``,``), and where blank-node groups
nest (``[ ]``). The layout pass walks the token stream and emits canonical
whitespace between tokens — it never reorders, drops, or rewrites token
content.

This trades semantic precision for two things:

1. **Comment preservation.** A full parser drops comments (rdflib does);
   a token stream keeps them at the position the author wrote them.
2. **String-literal fidelity.** Triple-quoted SPARQL bodies inside Turtle,
   raw string contents inside SPARQL — never reflowed, never touched.

The token taxonomy below covers both Turtle and SPARQL. SPARQL adds a few
keywords (``SELECT``, ``WHERE``, ``FILTER``, …) which the SPARQL layout
pass recognises by string-matching the ``IDENT`` token's text; the lexer
itself doesn't need to know about them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TT(str, Enum):
    """Token types.

    ``PREFIX_DECL`` matches both Turtle's ``@prefix`` and SPARQL's
    ``PREFIX``. The layout pass distinguishes by context (it knows which
    language it's formatting).
    """

    PREFIX_DECL = "prefix-decl"  # @prefix foo: <...> .  OR  PREFIX foo: <...>
    BASE_DECL = "base-decl"  # @base <...> .  OR  BASE <...>
    IRI = "iri"  # <https://example.org/foo>
    PNAME = "pname"  # foo:bar  (including default-empty :bar and a)
    BLANK = "blank"  # _:bnode_id
    VARIABLE = "variable"  # ?var or $var (SPARQL only — TTL never has these)
    STRING = "string"  # "..."  """..."""  '...'  '''...'''  (with optional @lang or ^^datatype suffix)
    NUMBER = "number"  # 42  3.14  6.022e23
    BOOL = "bool"  # true | false
    LBRACK = "lbrack"  # [
    RBRACK = "rbrack"  # ]
    LPAREN = "lparen"  # (
    RPAREN = "rparen"  # )
    LBRACE = "lbrace"  # {
    RBRACE = "rbrace"  # }
    SEMI = "semi"  # ;
    COMMA = "comma"  # ,
    DOT = "dot"  # .
    COMMENT = "comment"  # # ... (to end of line)
    NEWLINE = "newline"  # \n (only emitted when there's a blank line — see below)
    KEYWORD = "keyword"  # SPARQL keywords (SELECT, WHERE, ...); upper-case canonical
    IDENT = "ident"  # bare identifier (rare; used for SPARQL functions etc.)
    QT_OPEN = "qt-open"  # <<  — RDF-star quoted/reifier triple open
    QT_CLOSE = "qt-close"  # >>  — RDF-star quoted/reifier triple close
    TT_OPEN = "tt-open"  # <<(  — RDF-star triple-term open
    TT_CLOSE = "tt-close"  # )>>  — RDF-star triple-term close
    PATH_OP = "path-op"  # / | ^ * + ?  in SPARQL property paths
    OP = (
        "op"  # operators inside FILTER/BIND expressions: = != < <= > >= && || ! + - * /
    )


@dataclass(frozen=True)
class Token:
    """One token. ``text`` is the verbatim source — string-quote marks,
    angle brackets, etc. preserved. ``leading_blank_lines`` is the count of
    blank lines that appeared between the previous token and this one in
    the source, which the layout pass uses to detect author-intended
    paragraph breaks inside string literals or comment blocks. (It's never
    used outside comments — canonical whitespace is computed by layout.)
    """

    type: TT
    text: str
    line: int
    col: int
    leading_blank_lines: int = 0


# Single-character punctuation table. Order doesn't matter — looked up by
# the source char directly.
_PUNCT: dict[str, TT] = {
    "[": TT.LBRACK,
    "]": TT.RBRACK,
    "(": TT.LPAREN,
    ")": TT.RPAREN,
    "{": TT.LBRACE,
    "}": TT.RBRACE,
    ";": TT.SEMI,
    ",": TT.COMMA,
}


# Characters that are valid in the *body* of a prefixed name's local part.
# Turtle's PN_LOCAL allows a fairly wide range including `.`, `-`, `:`, and
# percent-encoded escapes. Stay conservative — only tokenise enough to handle
# typical Poliglot matrix sources, not to validate arbitrary Turtle. Anything
# unrecognised falls back to single-char punctuation or errors visibly
# (the formatter is a no-op on un-lex-able input — callers check round-trip
# equivalence before writing).
_PN_LOCAL_BODY = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _is_pn_chars_start(c: str) -> bool:
    """Start char of a PN_LOCAL (Turtle BNF). Accepts ASCII letters,
    underscore, and digits — broad enough for every prefix label in this
    codebase. (The full BNF accepts a large Unicode range not needed here.)"""
    return c.isalpha() or c == "_" or c.isdigit()


def _read_string_literal(source: str, i: int) -> tuple[str, int]:
    """Starting at ``i`` (which points at ``"`` or ``'``), read a complete
    string literal — single-line or triple-quoted — including any trailing
    ``@lang`` or ``^^datatype`` suffix. Returns (verbatim_text, new_i).

    The verbatim text includes the surrounding quotes, language tag, and
    datatype reference. The layout pass treats this as one indivisible
    token so triple-quoted SPARQL bodies survive intact.
    """
    quote = source[i]
    # Triple-quoted?
    if source[i : i + 3] == quote * 3:
        end = source.find(quote * 3, i + 3)
        if end == -1:
            msg = f"unterminated triple-quoted string starting at offset {i}"
            raise ValueError(msg)
        end += 3
    else:
        # Single-line. Walk forward, honouring backslash escapes.
        j = i + 1
        while j < len(source):
            c = source[j]
            if c == "\\":
                j += 2  # skip escaped char
                continue
            if c == quote:
                j += 1
                break
            if c == "\n":
                msg = f"unterminated single-line string at offset {i}"
                raise ValueError(msg)
            j += 1
        else:
            msg = f"unterminated string at offset {i}"
            raise ValueError(msg)
        end = j
    # Trailing language tag or datatype.
    if end < len(source) and source[end] == "@":
        # @en, @en-US
        end += 1
        while end < len(source) and (source[end].isalnum() or source[end] == "-"):
            end += 1
    elif source[end : end + 2] == "^^":
        end += 2
        # Datatype is either an IRI or a prefixed name. Recurse-by-loop.
        if source[end] == "<":
            close = source.find(">", end)
            if close == -1:
                msg = f"unterminated datatype IRI at offset {end}"
                raise ValueError(msg)
            end = close + 1
        else:
            # Prefixed name.
            while end < len(source) and (
                source[end].isalnum() or source[end] in "_-:."
            ):
                end += 1
    return source[i:end], end


def _read_iri(source: str, i: int) -> tuple[str, int]:
    """Read a ``<...>`` IRI starting at i (which points at ``<``). Returns
    (verbatim_text_incl_brackets, new_i). Newlines aren't allowed inside
    IRIs per spec; we error on them.
    """
    end = source.find(">", i + 1)
    if end == -1:
        msg = f"unterminated IRI at offset {i}"
        raise ValueError(msg)
    if "\n" in source[i:end]:
        msg = f"newline inside IRI at offset {i}"
        raise ValueError(msg)
    return source[i : end + 1], end + 1


def _read_pname(source: str, i: int) -> tuple[str, int]:
    """Read a prefixed name (``foo:bar``, ``:bar``, or the bare ``a``
    keyword). Returns (text, new_i).

    Caller has already established that the first char qualifies as a
    PN_PREFIX or PN_LOCAL start. We accept identifier chars, then
    optionally a single ``:`` followed by more identifier chars; the
    ``:`` is required for a prefixed name but absent for the bare ``a``
    case (which the layout pass treats as ``rdf:type``).
    """
    start = i
    while i < len(source) and (source[i].isalnum() or source[i] in "_-"):
        i += 1
    if i < len(source) and source[i] == ":":
        i += 1
        # Local part — may be empty (the prefix-only case is rare but legal).
        while i < len(source) and source[i] in _PN_LOCAL_BODY:
            i += 1
        # Trim trailing `.` — `foo:bar.` in TTL means `foo:bar` followed by
        # a statement terminator, not a local name ending in `.`. The PN_LOCAL
        # BNF technically allows interior dots but not a trailing one.
        while i > start and source[i - 1] == ".":
            i -= 1
    return source[start:i], i


def _read_number(source: str, i: int) -> tuple[str, int]:
    """Read an integer / decimal / double literal starting at ``i``. The
    leading char must already be a digit or ``+``/``-`` followed by a
    digit. Returns (text, new_i). Conservative: anything we don't match
    cleanly falls back to identifier and gets handled elsewhere.
    """
    start = i
    if source[i] in "+-":
        i += 1
    while i < len(source) and source[i].isdigit():
        i += 1
    if i < len(source) and source[i] == ".":
        if i + 1 < len(source) and source[i + 1].isdigit():
            i += 1
            while i < len(source) and source[i].isdigit():
                i += 1
    if i < len(source) and source[i] in "eE":
        i += 1
        if i < len(source) and source[i] in "+-":
            i += 1
        while i < len(source) and source[i].isdigit():
            i += 1
    return source[start:i], i


def tokenize(source: str, *, sparql_mode: bool = False) -> list[Token]:
    """Lex ``source`` into a list of tokens. ``sparql_mode`` affects only
    the recognition of ``?var`` (variables — SPARQL only) and ``BASE`` /
    ``PREFIX`` SPARQL keywords (Turtle uses ``@base`` / ``@prefix``).

    Errors are raised as ``ValueError`` with the offset. Callers (the CLI
    and LSP formatting commands) catch these and fall back to passing the
    source through unchanged — a formatter must never destroy input it
    can't parse.
    """
    tokens: list[Token] = []
    i = 0
    line = 1
    col = 1
    # Track blank-line count between tokens — used by the layout pass to
    # decide whether to preserve a comment as "trailing on previous line"
    # vs. "leading the next line".
    blank_lines_since_last_token = 0
    newline_since_last_token = False

    while i < len(source):
        c = source[i]

        # Whitespace.
        if c == "\n":
            if newline_since_last_token:
                blank_lines_since_last_token += 1
            newline_since_last_token = True
            i += 1
            line += 1
            col = 1
            continue
        if c in " \t\r":
            i += 1
            col += 1
            continue

        # Comments.
        if c == "#":
            end = source.find("\n", i)
            if end == -1:
                end = len(source)
            tokens.append(
                Token(
                    TT.COMMENT,
                    source[i:end],
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            i = end
            col = 1
            continue

        # @prefix / @base (Turtle directives).
        if c == "@":
            j = i + 1
            while j < len(source) and source[j].isalpha():
                j += 1
            directive = source[i:j]
            if directive == "@prefix":
                # Eat until the closing '.'.
                end = _scan_to_dot(source, j)
                tokens.append(
                    Token(
                        TT.PREFIX_DECL,
                        source[i:end].strip(),
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
                blank_lines_since_last_token = 0
                newline_since_last_token = False
                _update_pos_after = source[i:end]
                line += _update_pos_after.count("\n")
                col = (
                    end - _update_pos_after.rfind("\n")
                    if "\n" in _update_pos_after
                    else col + (end - i)
                )
                i = end
                continue
            if directive == "@base":
                end = _scan_to_dot(source, j)
                tokens.append(
                    Token(
                        TT.BASE_DECL,
                        source[i:end].strip(),
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
                blank_lines_since_last_token = 0
                newline_since_last_token = False
                _update_pos_after = source[i:end]
                line += _update_pos_after.count("\n")
                col = col + (end - i)
                i = end
                continue
            # Unknown @directive — fall through as IDENT so the layout
            # pass can surface the surprise.
            tokens.append(
                Token(
                    TT.IDENT,
                    directive,
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        # PREFIX / BASE (SPARQL directives — case-insensitive).
        if sparql_mode and source[i : i + 7].upper() == "PREFIX ":
            end = _scan_sparql_prefix_decl(source, i)
            tokens.append(
                Token(
                    TT.PREFIX_DECL,
                    source[i:end].strip(),
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            _segment = source[i:end]
            line += _segment.count("\n")
            col = col + (end - i)
            i = end
            continue
        if sparql_mode and source[i : i + 5].upper() == "BASE ":
            end = _scan_sparql_base_decl(source, i)
            tokens.append(
                Token(
                    TT.BASE_DECL,
                    source[i:end].strip(),
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            _segment = source[i:end]
            line += _segment.count("\n")
            col = col + (end - i)
            i = end
            continue

        # RDF-star delimiters (SPARQL 1.2 / RDF-star). These are atomic and
        # must be recognised before the single-char ``<`` IRI / ``>`` operator
        # and ``)`` punctuation branches, otherwise the IRI reader would
        # greedily swallow ``<<`` as the start of an ``<...>`` IRI and the
        # operator pass would split ``>>`` into two ``>`` tokens. The inner
        # ``s p o`` terms tokenise normally between the open and close.
        #
        # Triple-term open ``<<(`` must be tested before reifier open ``<<``
        # (longer match wins); likewise triple-term close ``)>>`` before the
        # bare ``)`` punctuation, and reifier close ``>>`` is its own token.
        if source[i : i + 3] == "<<(":
            tokens.append(
                Token(TT.TT_OPEN, "<<(", line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 3
            i += 3
            continue
        if source[i : i + 2] == "<<":
            tokens.append(
                Token(TT.QT_OPEN, "<<", line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 2
            i += 2
            continue
        if source[i : i + 3] == ")>>":
            tokens.append(
                Token(TT.TT_CLOSE, ")>>", line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 3
            i += 3
            continue
        if source[i : i + 2] == ">>":
            tokens.append(
                Token(TT.QT_CLOSE, ">>", line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 2
            i += 2
            continue

        # IRI.
        if c == "<":
            text, j = _read_iri(source, i)
            tokens.append(Token(TT.IRI, text, line, col, blank_lines_since_last_token))
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        # String.
        if c in "\"'":
            text, j = _read_string_literal(source, i)
            segment = source[i:j]
            tokens.append(
                Token(TT.STRING, text, line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            if "\n" in segment:
                line += segment.count("\n")
                col = j - source.rfind("\n", 0, j)
            else:
                col += j - i
            i = j
            continue

        # Blank node label.
        if source[i : i + 2] == "_:":
            j = i + 2
            while j < len(source) and (source[j].isalnum() or source[j] in "_-."):
                j += 1
            tokens.append(
                Token(
                    TT.BLANK,
                    source[i:j],
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        # Variable (SPARQL only).
        if sparql_mode and c in "?$":
            j = i + 1
            while j < len(source) and (source[j].isalnum() or source[j] == "_"):
                j += 1
            if j > i + 1:
                # Normalise $foo → ?foo (SPARQL allows both; we pick one).
                norm = "?" + source[i + 1 : j]
                tokens.append(
                    Token(
                        TT.VARIABLE,
                        norm,
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
                blank_lines_since_last_token = 0
                newline_since_last_token = False
                col += j - i
                i = j
                continue
            # Bare ? without a name is a property-path operator (zero-or-one).
            # Fall through to operator handling below.

        # Number (digit or signed-digit).
        if c.isdigit() or (
            c in "+-" and i + 1 < len(source) and source[i + 1].isdigit()
        ):
            text, j = _read_number(source, i)
            tokens.append(
                Token(TT.NUMBER, text, line, col, blank_lines_since_last_token)
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        # Punctuation.
        if c in _PUNCT:
            tokens.append(
                Token(
                    _PUNCT[c],
                    c,
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 1
            i += 1
            continue
        if c == ".":
            tokens.append(Token(TT.DOT, c, line, col, blank_lines_since_last_token))
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += 1
            i += 1
            continue

        # Property-path operators / SPARQL expression operators.
        if sparql_mode:
            two = source[i : i + 2]
            if two in {"&&", "||", "!=", "<=", ">=", "^^"}:
                tokens.append(
                    Token(TT.OP, two, line, col, blank_lines_since_last_token)
                )
                blank_lines_since_last_token = 0
                newline_since_last_token = False
                col += 2
                i += 2
                continue
            if c in "/^*+?!|=<>-":
                tokens.append(
                    Token(
                        TT.PATH_OP if c in "/^*+?|" else TT.OP,
                        c,
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
                blank_lines_since_last_token = 0
                newline_since_last_token = False
                col += 1
                i += 1
                continue

        # Bare `a` (Turtle alias for rdf:type) and prefixed names.
        if c.isalpha() or c == "_":
            text, j = _read_pname(source, i)
            if text == "a" and not (j < len(source) and source[j] == ":"):
                # Bare `a` — record as PNAME with text "a"; the layout pass
                # will keep it lowercase and treat it as rdf:type alias.
                tokens.append(
                    Token(
                        TT.PNAME,
                        "a",
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
            elif text.lower() in {"true", "false"} and ":" not in text:
                tokens.append(
                    Token(
                        TT.BOOL,
                        text.lower(),
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
            elif ":" in text:
                tokens.append(
                    Token(
                        TT.PNAME,
                        text,
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
            else:
                # Bare identifier — SPARQL keyword or function name.
                tokens.append(
                    Token(
                        TT.KEYWORD if sparql_mode else TT.IDENT,
                        text.upper() if sparql_mode else text,
                        line,
                        col,
                        blank_lines_since_last_token,
                    )
                )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        # Default-empty prefix (e.g. `:Foo`) starts with ':'.
        if c == ":":
            j = i + 1
            while j < len(source) and source[j] in _PN_LOCAL_BODY:
                j += 1
            while j > i + 1 and source[j - 1] == ".":
                j -= 1
            tokens.append(
                Token(
                    TT.PNAME,
                    source[i:j],
                    line,
                    col,
                    blank_lines_since_last_token,
                )
            )
            blank_lines_since_last_token = 0
            newline_since_last_token = False
            col += j - i
            i = j
            continue

        msg = (
            f"unexpected character {c!r} at line {line}, col {col} "
            f"(offset {i}) — formatter aborting; source returned unchanged"
        )
        raise ValueError(msg)

    return tokens


def _scan_to_dot(source: str, i: int) -> int:
    """Find the closing ``.`` of a TTL ``@prefix`` / ``@base`` declaration,
    respecting nested IRI/string boundaries (a ``.`` inside ``<>`` or
    ``""`` doesn't terminate)."""
    while i < len(source):
        c = source[i]
        if c == "<":
            close = source.find(">", i)
            if close == -1:
                msg = "unterminated IRI in directive"
                raise ValueError(msg)
            i = close + 1
        elif c in "\"'":
            _, i = _read_string_literal(source, i)
        elif c == ".":
            return i + 1
        else:
            i += 1
    msg = "unterminated directive (no closing dot)"
    raise ValueError(msg)


def _scan_sparql_prefix_decl(source: str, i: int) -> int:
    """SPARQL ``PREFIX foo: <...>`` ends at the closing ``>`` (no trailing
    dot in SPARQL syntax). Returns the offset *after* the ``>``."""
    # Skip the PREFIX keyword.
    i += len("PREFIX")
    # Find the IRI's closing bracket.
    open_idx = source.find("<", i)
    close = source.find(">", open_idx) if open_idx != -1 else -1
    if close == -1:
        msg = "unterminated SPARQL PREFIX declaration"
        raise ValueError(msg)
    return close + 1


def _scan_sparql_base_decl(source: str, i: int) -> int:
    i += len("BASE")
    open_idx = source.find("<", i)
    close = source.find(">", open_idx) if open_idx != -1 else -1
    if close == -1:
        msg = "unterminated SPARQL BASE declaration"
        raise ValueError(msg)
    return close + 1
