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


def test_comment_inside_nested_blank_node_does_not_crash() -> None:
    """A comment placed inside a nested blank-node predicate list must be
    emitted as a standalone line, not folded into a predicate-object group.

    Regression: the nested-blank-node body emitter did not split comments
    out of predicate-object groups (the top-level emitter does). A comment
    sitting as the last item of a nested blank node (after a ``;``) became a
    group whose only token was the comment, leaving the object list empty.
    When the comment text was long enough to defeat the inline-join branch,
    the multi-object fallback indexed ``objects[0]`` on that empty list and
    raised ``IndexError``."""
    long_comment = (
        "# this comment is intentionally longer than one hundred columns so "
        "the inline path is skipped here ok"
    )
    src = (
        "@prefix ex: <http://example.org/> .\n"
        "\n"
        "ex:thing\n"
        "    ex:p [\n"
        "        ex:c [\n"
        "            ex:d ex:e ;\n"
        f"            {long_comment}\n"
        "        ]\n"
        "    ] .\n"
    )

    # Must not raise.
    result = format_turtle(src)

    # The comment survives verbatim, on its own line.
    assert long_comment in result
    # The data triple is intact (the comment did NOT absorb it).
    assert "ex:d ex:e" in result


def test_comment_between_predicates_in_nested_blank_node_is_standalone() -> None:
    """A comment between two predicates inside a nested blank node stays on
    its own line and does not swallow the following predicate-object group.

    Regression: a short comment used to be folded onto the previous line,
    gluing the following predicate text into the comment (data corruption)
    and breaking idempotency."""
    src = (
        "@prefix ex: <http://example.org/> .\n"
        "\n"
        "ex:thing\n"
        "    ex:p [\n"
        "        ex:a ex:b ;\n"
        "        # a comment between predicates\n"
        "        ex:c [\n"
        "            ex:k ex:v ;\n"
        "            ex:k ex:w\n"
        "        ] ;\n"
        "        ex:f ex:g\n"
        "    ] .\n"
    )

    result = format_turtle(src)

    # The comment is preserved on its own line, with no following content
    # glued onto it.
    assert "# a comment between predicates\n" in result
    # Every triple survives.
    for fragment in ("ex:a ex:b", "ex:k ex:v", "ex:k ex:w", "ex:f ex:g"):
        assert fragment in result


def test_nested_blank_node_comment_is_idempotent() -> None:
    """``format_turtle`` must be idempotent on Turtle that places comments
    inside nested blank-node predicate lists: reformatting its own output
    yields identical text and never crashes."""
    src = (
        "@prefix ex: <http://example.org/> .\n"
        "\n"
        "ex:thing\n"
        "    ex:p [\n"
        "        ex:a ex:b ;\n"
        "        # leading comment for the nested group\n"
        "        ex:c [\n"
        "            ex:d ex:e ;\n"
        "            # trailing comment as the last item in the nested bnode\n"
        "        ] ;\n"
        "        ex:f ex:g\n"
        "    ] .\n"
    )

    once = format_turtle(src)
    twice = format_turtle(once)
    assert once == twice, (
        "formatter is not idempotent on nested-blank-node comments:\n"
        f"--- first pass ---\n{once}\n--- second pass ---\n{twice}"
    )
