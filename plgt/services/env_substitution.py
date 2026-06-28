"""Environment-variable substitution in RDF literal objects at build time.

Spec literals may carry deployment-specific values that should come from the build
environment rather than being hardcoded — typically endpoint URLs that differ per
environment, e.g.::

    ex:endpointUrl  "${API_BASE_URL:http://localhost:8080}/v1"^^xsd:anyURI

``plgt build`` resolves these placeholders so a package built in a given environment
picks up that environment's values, and falls back to the literal's default otherwise.

Substitution is per-occurrence anywhere inside a literal — placeholders are usually
embedded substrings (``${VAR}/path``), not standalone literals — and applies only to
RDF literal objects, so IRIs, prefixes and comments are never touched. Grammar and
precedence: ``${VAR}`` or ``${VAR:default}``; the replacement is the environment value
if set, otherwise the default, otherwise the empty string.
"""

from __future__ import annotations

import os
import re

from rdflib import Graph, Literal

# Matches ${VAR_NAME} or ${VAR_NAME:default_value}: the name is everything up to a ':'
# or '}', and the optional default is everything up to the closing '}'.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def substitute_env_text(text: str) -> str:
    """Replace every ``${VAR}`` / ``${VAR:default}`` placeholder in ``text``.

    Precedence per placeholder: environment value → default → empty string.
    Text without a placeholder is returned unchanged.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None when the ":default" group is absent
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        return default_value if default_value is not None else ""

    return _ENV_VAR_PATTERN.sub(_replace, text)


def substitute_env_vars(graph: Graph) -> Graph:
    """Substitute environment placeholders in every literal object of ``graph``.

    Mutates ``graph`` in place (rdflib graphs aren't copy-cheap) and returns it for
    chaining, matching :func:`plgt.services.script_expander.expand_script_refs`. The
    datatype and language tag of each rewritten literal are preserved.
    """
    # Collect first, then mutate — never modify a graph mid-iteration.
    to_replace: list[tuple[object, object, Literal, str]] = []
    for subject, predicate, obj in graph:
        if not isinstance(obj, Literal):
            continue
        lexical = str(obj)
        if "${" not in lexical:
            continue
        substituted = substitute_env_text(lexical)
        if substituted != lexical:
            to_replace.append((subject, predicate, obj, substituted))

    for subject, predicate, obj, substituted in to_replace:
        graph.remove((subject, predicate, obj))
        # A literal carries at most one of (datatype, language); passing both with the
        # unused one as None reproduces the original literal's type exactly.
        graph.add(
            (
                subject,
                predicate,
                Literal(substituted, datatype=obj.datatype, lang=obj.language),
            )
        )

    return graph
