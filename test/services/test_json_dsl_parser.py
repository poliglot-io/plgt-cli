"""Unit tests for ``plgt.services.json_dsl_parser``.

Mirrors the authoritative server-side test surface at a level sufficient to
catch drift if the grammar evolves. The end-to-end firing of these errors via
``validate_project`` is covered by ``test_broken_fixtures.py`` (the parser is
the upstream of that surface).
"""

from __future__ import annotations

import pytest
from plgt.services.json_dsl_parser import JsonDslParseError, parse


class TestWellFormed:
    def test_minimal_object(self) -> None:
        parse('JSON { "x": 1 }')

    def test_minimal_array(self) -> None:
        parse("JSON [ 1, 2, 3 ]")

    def test_with_prefixes(self) -> None:
        parse(
            'PREFIX ex: <https://example.com/#>\nJSON { "x": ?v } WHERE { ?v ex:p ?o }'
        )

    def test_nested_objects_and_arrays(self) -> None:
        parse(
            'JSON {  "a": [1, 2, {"b": ?x}],  "c": {"d": "string"}} WHERE { ?x ?p ?o }'
        )

    def test_where_with_nested_braces(self) -> None:
        # SPARQL OPTIONAL { … } and FILTER (… {…} …) nested inside WHERE.
        parse('JSON { "x": ?v } WHERE { OPTIONAL { ?v ?p ?o } }')

    def test_empty_object_and_array(self) -> None:
        parse("JSON {}")
        parse("JSON []")

    def test_all_literal_types(self) -> None:
        parse(
            "JSON {"
            '  "s": "hello",'
            '  "n": 42,'
            '  "d": -3.14,'
            '  "b": true,'
            '  "z": false,'
            '  "u": null'
            "}"
        )

    def test_string_escape_sequences(self) -> None:
        # All five valid JSON DSL escapes.
        parse(r'JSON { "a": "line\nbreak\ttab\rcr\"q\\bs" }')


class TestParseErrors:
    def test_missing_json_keyword(self) -> None:
        with pytest.raises(JsonDslParseError, match="Expected 'JSON'"):
            parse('{ "x": 1 }')

    def test_missing_brace_after_json(self) -> None:
        with pytest.raises(JsonDslParseError, match=r"Expected '\{' or '\["):
            parse("JSON x")

    def test_unbalanced_object(self) -> None:
        # Missing closing brace surfaces as end-of-input within member loop.
        with pytest.raises(JsonDslParseError):
            parse('JSON { "x": 1')

    def test_unbalanced_array(self) -> None:
        with pytest.raises(JsonDslParseError):
            parse("JSON [ 1, 2")

    def test_invalid_escape_sequence(self) -> None:
        with pytest.raises(JsonDslParseError, match="Invalid escape sequence"):
            parse(r'JSON { "x": "\q" }')

    def test_unterminated_escape_sequence(self) -> None:
        with pytest.raises(JsonDslParseError, match="Unterminated escape sequence"):
            parse('JSON { "x": "\\')

    def test_multiple_decimal_points(self) -> None:
        with pytest.raises(JsonDslParseError, match="Multiple decimal points"):
            parse('JSON { "x": 1.2.3 }')

    def test_boolean_typo(self) -> None:
        with pytest.raises(JsonDslParseError, match="Expected 'true' or 'false'"):
            parse('JSON { "x": truee }')

    def test_variable_starts_with_digit(self) -> None:
        with pytest.raises(JsonDslParseError, match="must start with a letter"):
            parse('JSON { "x": ?1bad }')

    def test_missing_colon_in_member(self) -> None:
        with pytest.raises(JsonDslParseError, match="Expected ':'"):
            parse('JSON { "x" 1 }')

    def test_double_comma_in_object(self) -> None:
        # ", ," would be parsed as: member done, then expect a key, find ','
        # → "Expected string key".
        with pytest.raises(JsonDslParseError, match="Expected string key"):
            parse('JSON { "x": 1, , "y": 2 }')

    def test_unclosed_where(self) -> None:
        with pytest.raises(JsonDslParseError, match="Unclosed WHERE clause"):
            parse('JSON { "x": ?v } WHERE { ?v ?p ?o')

    def test_content_after_value(self) -> None:
        # Trailing junk after the top-level value (and any WHERE) is an error.
        with pytest.raises(JsonDslParseError, match="Unexpected content"):
            parse('JSON { "x": 1 } trailing-junk')


class TestVariableExtraction:
    def test_value_vars_collected(self) -> None:
        result = parse('JSON { "x": ?a, "y": ?b }')
        assert result.value_vars == {"a", "b"}
        assert result.where_vars == set()

    def test_where_vars_collected(self) -> None:
        result = parse('JSON { "x": ?a } WHERE { ?a ?p ?b . ?b ?q ?c }')
        assert result.value_vars == {"a"}
        # SPARQL ?vars inside WHERE all collected.
        assert result.where_vars == {"a", "p", "b", "q", "c"}

    def test_nested_object_where(self) -> None:
        # A nested object's WHERE puts its vars into the same where_vars
        # pool — scope-strictness can layer on top later.
        result = parse(
            'JSON { "inner": {"x": ?nx} WHERE { ?nx ?p ?o } } WHERE { ?z ?q ?r }'
        )
        assert result.value_vars == {"nx"}
        assert "nx" in result.where_vars
        assert "z" in result.where_vars

    def test_string_vars_not_captured(self) -> None:
        # ?var inside a string literal is content, not a variable reference.
        result = parse('JSON { "x": "?notAVariable" }')
        assert result.value_vars == set()


class TestPositionTracking:
    def test_error_line_column(self) -> None:
        # Error on the second line should report line=2.
        with pytest.raises(JsonDslParseError) as exc_info:
            parse('JSON {\n  "x" 1 }')
        assert exc_info.value.line == 2
        assert exc_info.value.column >= 1
