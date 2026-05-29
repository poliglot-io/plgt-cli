"""Recursive-descent parser for the JSON DSL.

Mirrors the authoritative server-side grammar; this CLI-side port exists so
that ``plgt validate`` and the LSP can flag the same syntax errors at authoring
time instead of the platform discovering them at install.

Grammar (excerpt from the authoritative parser's docs)::

    JsonDsl       ::= Prefixes? JsonValue
    Prefixes      ::= Prefix+
    Prefix        ::= "PREFIX" prefixLabel ":" "<" iri ">"
    JsonValue     ::= "JSON" ( JsonObjectBody | JsonArrayBody )
    JsonObjectBody::= "{" Members? "}" WhereClause?
    JsonArrayBody ::= "[" ArrayElements? "]" WhereClause?
    Members       ::= Member ( "," Member )*
    Member        ::= String ":" Value
    Value         ::= Variable | String | Number | Boolean | "null"
                    | "{" Members? "}" WhereClause?
                    | "[" ArrayElements? "]" WhereClause?
    ArrayElements ::= ArrayElement ( "," ArrayElement )*
    ArrayElement  ::= Variable | String | Number | Boolean | "null"
                    | "{" Members? "}" WhereClause?
                    | "[" ArrayElements? "]" WhereClause?
    Variable      ::= "?" Letter ( Letter | Digit | "_" )*
    WhereClause   ::= "WHERE" "{" sparql_balanced "}"

The "JSON" keyword is only required at the root level — nested objects /
arrays use bare ``{…}`` / ``[…]``. WHERE clause content is captured verbatim
with brace counting (we don't parse SPARQL here).

We surface two pieces of information for the CLI's validator:

* parse errors as ``JsonDslParseError`` exceptions with line/column,
* the sets of variables used in the value part and bound in any
  ``WHERE`` clause, so the caller can flag unbound variables (the server
  catches those at execution, not parse time, which is why this scoping
  check lives here instead of inside the parser itself).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NoReturn


@dataclass
class JsonDslParseError(Exception):
    """A syntax error from the JSON DSL parser. Carries 1-based line/column
    pointing at the position where the parser detected the problem.
    """

    message: str
    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.message} (line {self.line}, column {self.column})"


@dataclass
class ParseResult:
    """Outcome of parsing a JSON DSL body.

    * ``value_vars`` — every ``?var`` referenced outside any ``WHERE`` clause.
    * ``where_vars`` — every ``?var`` referenced inside any ``WHERE`` clause
      (any depth — WHERE clauses can nest under objects / arrays).
    """

    value_vars: set[str] = field(default_factory=set)
    where_vars: set[str] = field(default_factory=set)


# Standard SPARQL-keyword tokens that must not be misinterpreted as the WHERE
# keyword opener when scanning. Only WHERE matters for our nested-clause
# detection; the rest is opaque inside WHERE braces.
_WHITESPACE = " \t\r\n\f\v"


class JsonDslParser:
    """Recursive-descent JSON DSL parser.

    Usage::

        result = JsonDslParser(body).parse()

    Raises ``JsonDslParseError`` on syntax errors. Returns a
    :class:`ParseResult` with the variable sets the caller needs for
    scope checking.
    """

    def __init__(self, source: str) -> None:
        self._src = source
        self._pos = 0
        self._line = 1
        self._col = 1
        self._result = ParseResult()
        # Reference into _result so nested helpers don't need to thread a
        # "currently inside WHERE?" flag — they pick the right set up front.
        self._cur_vars = self._result.value_vars

    # -- public ---------------------------------------------------------

    def parse(self) -> ParseResult:
        self._skip_ws()
        self._extract_prefixes()
        self._skip_ws()
        self._parse_value()
        self._skip_ws()
        if self._pos < len(self._src):
            self._error(f"Unexpected content after JSON DSL: {self._peek()!r}")
        return self._result

    # -- top-level helpers ----------------------------------------------

    def _extract_prefixes(self) -> None:
        """Strip leading ``PREFIX <name>: <iri>`` declarations. We don't keep
        them — the validator already checks prefixed-name validity via the
        graph's namespace table — but consuming them keeps the parser's
        position aligned with the authoritative parser.
        """
        while self._match_keyword("PREFIX"):
            self._consume_keyword("PREFIX")
            self._skip_ws()
            # prefix label up to ':'
            while self._pos < len(self._src) and self._peek() != ":":
                self._consume()
            if self._peek() == ":":
                self._consume()
            self._skip_ws()
            # IRI in angle brackets
            if self._peek() != "<":
                self._error("Expected '<iri>' after PREFIX label")
            self._consume()  # '<'
            while self._pos < len(self._src) and self._peek() != ">":
                self._consume()
            if self._peek() != ">":
                self._error("Unterminated IRI in PREFIX declaration")
            self._consume()  # '>'
            self._skip_ws()

    def _parse_value(self) -> None:
        if not self._match_keyword("JSON"):
            self._error("Expected 'JSON'")
        self._consume_keyword("JSON")
        self._skip_ws()
        c = self._peek()
        if c == "{":
            self._parse_object_body()
        elif c == "[":
            self._parse_array_body()
        else:
            self._error("Expected '{' or '[' after JSON")

    # -- structural ------------------------------------------------------

    def _parse_object_body(self) -> None:
        self._consume_char("{")
        self._parse_members()
        self._consume_char("}")
        self._parse_where()

    def _parse_array_body(self) -> None:
        self._consume_char("[")
        self._parse_array_elements()
        self._consume_char("]")
        self._parse_where()

    def _parse_members(self) -> None:
        self._skip_ws()
        if self._peek() == "}":
            return  # empty
        while True:
            self._skip_ws()
            # key
            if self._peek() != '"':
                self._error("Expected string key for object member")
            self._parse_string()
            self._skip_ws()
            self._consume_char(":")
            self._skip_ws()
            self._parse_member_value()
            self._skip_ws()
            c = self._peek()
            if c == ",":
                self._consume()
            elif c == "}":
                return
            else:
                self._error("Expected ',' or '}' after member")

    def _parse_array_elements(self) -> None:
        self._skip_ws()
        if self._peek() == "]":
            return  # empty
        while True:
            self._skip_ws()
            self._parse_array_element()
            self._skip_ws()
            c = self._peek()
            if c == ",":
                self._consume()
            elif c == "]":
                return
            else:
                self._error("Expected ',' or ']' after array element")

    def _parse_array_element(self) -> None:
        self._skip_ws()
        c = self._peek()
        if c == "{":
            self._consume()
            self._parse_members()
            self._consume_char("}")
            self._parse_where()
        elif c == "[":
            self._consume()
            self._parse_array_elements()
            self._consume_char("]")
            self._parse_where()
        elif c == "?":
            self._parse_variable()
        elif c == '"':
            self._parse_string()
        elif c in ("t", "f"):
            self._parse_boolean()
        elif c == "n":
            self._parse_null()
        elif c == "-" or c.isdigit():
            self._parse_number()
        else:
            self._error("Expected array element (variable, literal, or nested JSON)")

    def _parse_member_value(self) -> None:
        self._skip_ws()
        c = self._peek()
        if c == "?":
            self._parse_variable()
        elif c == '"':
            self._parse_string()
        elif c in ("t", "f"):
            self._parse_boolean()
        elif c == "n":
            self._parse_null()
        elif c == "-" or c.isdigit():
            self._parse_number()
        elif c == "{":
            self._consume()
            self._parse_members()
            self._consume_char("}")
            self._parse_where()
        elif c == "[":
            self._consume()
            self._parse_array_elements()
            self._consume_char("]")
            self._parse_where()
        else:
            self._error("Expected value (variable, literal, or nested object/array)")

    # -- primitives ------------------------------------------------------

    def _parse_variable(self) -> None:
        self._consume_char("?")
        if self._pos >= len(self._src) or not self._peek().isalpha():
            self._error("Variable name must start with a letter")
        start = self._pos
        while self._pos < len(self._src) and (
            self._peek().isalnum() or self._peek() == "_"
        ):
            self._consume()
        name = self._src[start : self._pos]
        self._cur_vars.add(name)

    def _parse_string(self) -> None:
        self._consume_char('"')
        while self._pos < len(self._src) and self._peek() != '"':
            c = self._consume()
            if c == "\\":
                if self._pos >= len(self._src):
                    self._error("Unterminated escape sequence")
                escaped = self._consume()
                if escaped not in ("n", "t", "r", '"', "\\"):
                    self._error(f"Invalid escape sequence: \\{escaped}")
        if self._pos >= len(self._src):
            self._error("Unterminated string literal")
        self._consume_char('"')

    def _parse_number(self) -> None:
        start = self._pos
        seen_decimal = False
        if self._peek() == "-":
            self._consume()
        while self._pos < len(self._src) and (
            self._peek().isdigit() or self._peek() == "."
        ):
            if self._peek() == ".":
                if seen_decimal:
                    self._error("Multiple decimal points in number")
                seen_decimal = True
            self._consume()
        num_str = self._src[start : self._pos]
        # Re-validate with Python's own number parser. Catches edge cases
        # like "-" or "-." that the digit-loop alone would accept.
        try:
            if seen_decimal:
                float(num_str)
            else:
                int(num_str)
        except ValueError as e:
            self._error(f"Invalid number: {num_str}", cause=e)

    def _parse_boolean(self) -> None:
        if self._match_keyword("true"):
            self._consume_keyword("true")
        elif self._match_keyword("false"):
            self._consume_keyword("false")
        else:
            self._error("Expected 'true' or 'false'")

    def _parse_null(self) -> None:
        if not self._match_keyword("null"):
            self._error("Expected 'null'")
        self._consume_keyword("null")

    def _parse_where(self) -> None:
        """Capture a ``WHERE { … }`` clause if present. Inside the clause we
        only track ``?var`` references — SPARQL itself is parsed elsewhere
        (rdflib for top-level scripts; the authoritative parser for JSON DSL bodies).
        """
        self._skip_ws()
        if not self._match_keyword("WHERE"):
            return
        self._consume_keyword("WHERE")
        self._skip_ws()
        self._consume_char("{")
        # Inside WHERE, ?vars go into the where_vars set.
        prev = self._cur_vars
        self._cur_vars = self._result.where_vars
        try:
            depth = 1
            while self._pos < len(self._src) and depth > 0:
                c = self._peek()
                if c == "{":
                    depth += 1
                    self._consume()
                elif c == "}":
                    depth -= 1
                    self._consume()
                elif c == "?":
                    # SPARQL-style ?var — same shape as JSON-DSL variables.
                    self._parse_sparql_var()
                elif c in ('"', "'"):
                    self._skip_sparql_string(c)
                elif c == "<":
                    self._skip_sparql_iri()
                else:
                    self._consume()
            if depth > 0:
                self._error("Unclosed WHERE clause")
        finally:
            self._cur_vars = prev

    def _parse_sparql_var(self) -> None:
        """Variable reference inside a WHERE clause. Allows the same name
        shape as JSON-DSL variables (letter-led, alphanumeric+underscore).
        Leading-underscore SPARQL-internal vars like ``?_process`` are valid
        in WHERE per the spec — accept them too.
        """
        self._consume_char("?")
        start = self._pos
        # SPARQL allows leading underscore in vars (e.g. ?_process); JSON
        # DSL value-side variables don't, but the binding side does.
        if self._pos >= len(self._src) or not (
            self._peek().isalpha() or self._peek() == "_"
        ):
            self._error("Variable name must start with a letter")
        while self._pos < len(self._src) and (
            self._peek().isalnum() or self._peek() == "_"
        ):
            self._consume()
        self._cur_vars.add(self._src[start : self._pos])

    def _skip_sparql_string(self, quote: str) -> None:
        """Skip past a single- or double-quoted SPARQL string literal so
        embedded braces / question marks inside it don't confuse the
        WHERE-balance scan.
        """
        self._consume_char(quote)
        while self._pos < len(self._src) and self._peek() != quote:
            if self._peek() == "\\" and self._pos + 1 < len(self._src):
                self._consume()
                self._consume()
            else:
                self._consume()
        if self._pos < len(self._src):
            self._consume_char(quote)

    def _skip_sparql_iri(self) -> None:
        """Skip past a ``<iri>`` token inside a WHERE clause. Doesn't
        validate the IRI shape — the SPARQL parser at install time covers
        that. We just need to swallow the closing ``>``.
        """
        self._consume_char("<")
        while self._pos < len(self._src) and self._peek() != ">":
            self._consume()
        if self._pos < len(self._src):
            self._consume_char(">")

    # -- low-level scanners ---------------------------------------------

    def _match_keyword(self, keyword: str) -> bool:
        end = self._pos + len(keyword)
        if end > len(self._src):
            return False
        if self._src[self._pos : end].lower() != keyword.lower():
            return False
        # Keyword boundary — next char (if any) must not extend the identifier.
        if end < len(self._src):
            nxt = self._src[end]
            if nxt.isalnum() or nxt == "_":
                return False
        return True

    def _consume_keyword(self, keyword: str) -> None:
        if not self._match_keyword(keyword):
            self._error(f"Expected {keyword!r}")
        for _ in range(len(keyword)):
            self._consume()

    def _peek(self) -> str:
        return self._src[self._pos] if self._pos < len(self._src) else "\0"

    def _consume(self) -> str:
        if self._pos >= len(self._src):
            self._error("Unexpected end of input")
        c = self._src[self._pos]
        self._pos += 1
        if c == "\n":
            self._line += 1
            self._col = 1
        else:
            self._col += 1
        return c

    def _consume_char(self, expected: str) -> None:
        self._skip_ws()
        if self._peek() != expected:
            self._error(f"Expected {expected!r} but found {self._peek()!r}")
        self._consume()

    def _skip_ws(self) -> None:
        while self._pos < len(self._src) and self._src[self._pos] in _WHITESPACE:
            self._consume()

    def _error(self, message: str, *, cause: BaseException | None = None) -> NoReturn:
        """Raise a parse error with current line/column. The helper raises
        rather than returning so callers don't need a redundant ``raise``
        keyword. Pass ``cause=`` to chain a low-level exception (e.g. a
        Python number-parse failure) so the traceback survives. The ruff
        exceptions are intentional here — parser error messages are
        inherently inline and one-off; collapsing them into a fixed-message
        exception class would obscure the grammar.
        """
        err = JsonDslParseError(message, self._line, self._col)
        if cause is not None:
            raise err from cause
        raise err


def parse(source: str) -> ParseResult:
    """Convenience: parse a JSON DSL body and return its ParseResult.

    Raises ``JsonDslParseError`` on syntax errors.
    """
    return JsonDslParser(source).parse()
