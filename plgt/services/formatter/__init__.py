"""Opinionated formatters for Turtle and SPARQL.

The formatter has one rule: canonical input → canonical output. There is no
configuration. This is intentional — the agent-write-then-format workflow
relies on every author (human or AI) producing identical bytes from the same
semantic input, so review diffs only reflect real changes.

Two entry points:

* :func:`format_turtle(source: str) -> str` — format `.ttl` text.
* :func:`format_sparql(source: str) -> str` — format `.rq` text. JSON-DSL
  bodies (``JSON { ... } WHERE { ... }``) are detected and only the WHERE
  portion is reformatted; the JSON payload is left verbatim.

Both are deterministic and idempotent: ``f(f(x)) == f(x)``. They preserve
comments at the position the author placed them and never reflow string
literal contents (including triple-quoted SPARQL bodies inside TTL).
"""

from plgt.services.formatter.sparql import format_sparql
from plgt.services.formatter.turtle import format_turtle

__all__ = ["format_sparql", "format_turtle"]
