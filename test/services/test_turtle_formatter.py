"""Regression tests for the Turtle formatter's comment handling.

Earlier the formatter treated every comment line as a standalone top-level
statement and inserted a blank line before each one. That dissolved section
banners and prose paragraphs into a stack of floating one-liners.

The rule now is: comments pass through verbatim — the formatter preserves
exactly the blank-line spacing the author wrote leading into each comment
line and into the next statement that follows a comment region. The
"statement → statement" path is unchanged (still one blank line between
named subject blocks).
"""

from plgt.services.formatter.turtle import format_turtle


def test_consecutive_comments_stay_tight() -> None:
    """A section banner (rule / title / rule) followed by a wrapped prose
    paragraph stays as one tight block — the formatter does NOT insert
    blank lines between adjacent comment lines."""
    src = (
        "@prefix : <http://example.com/> .\n"
        "\n"
        "# =============================================================================\n"
        "# Trust Policy Shape\n"
        "# =============================================================================\n"
        "# Closes a previously-unconstrained surface: TrustPolicy is defined in\n"
        "# access-control.ttl as an owl:Class but had no SHACL coverage.\n"
        "\n"
        ":Foo a :Bar .\n"
    )

    result = format_turtle(src)

    assert (
        "# =============================================================================\n"
        "# Trust Policy Shape\n"
        "# =============================================================================\n"
        "# Closes a previously-unconstrained surface: TrustPolicy is defined in\n"
        "# access-control.ttl as an owl:Class but had no SHACL coverage."
    ) in result


def test_author_blank_between_comments_is_preserved() -> None:
    """When the author *wrote* a blank line between two comment blocks,
    the formatter preserves it. Comments are author intent — the formatter
    neither adds nor strips blank lines around them."""
    src = (
        "@prefix : <http://example.com/> .\n"
        "\n"
        "# First block line 1.\n"
        "# First block line 2.\n"
        "\n"
        "# Second block line 1.\n"
        "# Second block line 2.\n"
        "\n"
        ":Foo a :Bar .\n"
    )

    result = format_turtle(src)

    # Each block stays tight internally, AND the author's blank line between
    # the two blocks is preserved verbatim.
    assert (
        "# First block line 1.\n"
        "# First block line 2.\n"
        "\n"
        "# Second block line 1.\n"
        "# Second block line 2."
    ) in result


def test_statement_then_comment_preserves_author_spacing() -> None:
    """A statement followed by a comment keeps whatever spacing the author
    wrote — not a forced blank line. If the author put two blank lines, we
    emit two; if they wrote it tight against the statement, we emit it tight."""
    src = (
        "@prefix : <http://example.com/> .\n"
        "\n"
        ":Foo a :Bar .\n"
        "\n"
        "\n"
        "# Section heading after two blanks.\n"
        ":Baz a :Qux .\n"
    )

    result = format_turtle(src)
    lines = result.splitlines()
    idx = next(
        i for i, line in enumerate(lines) if line.startswith("# Section heading")
    )
    # Two blank lines before the comment — author intent preserved.
    assert lines[idx - 1] == ""
    assert lines[idx - 2] == ""


def test_comment_then_statement_can_stay_tight() -> None:
    """A comment immediately followed by its target statement (no author
    blank line) stays tight. This is the typical inline-doc pattern:

        # Description of the next shape.
        :MyShape a sh:NodeShape ...
    """
    src = (
        "@prefix : <http://example.com/> .\n"
        "\n"
        "# Description of the next shape.\n"
        ":MyShape a :Bar .\n"
    )

    result = format_turtle(src)

    assert "# Description of the next shape.\n:MyShape" in result


def test_statement_to_statement_still_gets_one_blank() -> None:
    """The change is scoped to comment-adjacent transitions. Pure
    statement-to-statement transitions still get the standard one-blank-line
    separator regardless of what the author wrote (formatter still owns
    statement layout)."""
    src = "@prefix : <http://example.com/> .\n\n:Foo a :Bar .\n\n\n\n:Baz a :Qux .\n"

    result = format_turtle(src)
    # Exactly one blank line between the two subject blocks (the formatter
    # may split each block over multiple lines per its predicate-layout
    # rules, but only one blank-line gap separates them regardless of how
    # many blanks the author wrote).
    foo_end = result.index(":Bar .") + len(":Bar .")
    baz_start = result.index(":Baz")
    between = result[foo_end:baz_start]
    assert between == "\n\n", f"expected exactly one blank line gap, got: {between!r}"
