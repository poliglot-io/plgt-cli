"""RDF processing operations for matrix building.

This module contains pure functions for RDF file discovery, validation,
merging, and output operations.
"""

import logging
from pathlib import Path

from rdflib import Graph

from plgt.core import settings
from plgt.core.exceptions import CLIError

logger = logging.getLogger(settings.APP_AUTHOR)


class BuildError(CLIError):
    """Raised when build process encounters an error."""


def discover_rdf_files(spec_dir: Path) -> list[Path]:
    """Discover RDF files in the specification directory.

    Args:
        spec_dir: Path to the specification directory

    Returns:
        List of RDF file paths

    Raises:
        BuildError: If spec directory doesn't exist or no RDF files found
    """
    if not spec_dir.exists():
        msg = f"Specification directory does not exist: {spec_dir}"
        raise BuildError(msg)

    if not spec_dir.is_dir():
        msg = f"Specification path is not a directory: {spec_dir}"
        raise BuildError(msg)

    rdf_extensions = {".ttl", ".rdf", ".owl", ".nt", ".n3", ".jsonld"}
    rdf_files = []
    for ext in rdf_extensions:
        rdf_files.extend(spec_dir.glob(f"**/*{ext}"))

    if not rdf_files:
        msg = f"No RDF files found in directory: {spec_dir}"
        raise BuildError(msg)

    return sorted(rdf_files)


def validate_rdf_file(file_path: Path) -> tuple[bool, str | None]:
    """Validate an RDF file by attempting to parse it.

    Args:
        file_path: Path to the RDF file

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        graph = Graph()
        graph.parse(file_path, format=None)
        return True, None
    except (ValueError, OSError, RuntimeError, SyntaxError) as e:  # RDF parsing errors
        return False, str(e)


def merge_rdf_files(rdf_files: list[Path]) -> Graph:
    """Merge multiple RDF files into a single graph, preserving prefixes.

    Args:
        rdf_files: List of RDF file paths to merge

    Returns:
        Merged RDF graph with preserved namespace bindings

    Raises:
        BuildError: If merging fails
    """
    merged_graph = Graph()
    namespace_map: dict[str, str] = {}  # prefix -> namespace
    reverse_map: dict[str, set[str]] = {}  # namespace -> set of prefixes

    for file_path in rdf_files:
        try:
            graph = Graph()
            graph.parse(file_path, format=None)

            # Preserve namespace bindings from source files
            for prefix, namespace in graph.namespaces():
                ns_str = str(namespace)

                # Track all prefixes for each namespace
                if ns_str not in reverse_map:
                    reverse_map[ns_str] = set()
                reverse_map[ns_str].add(prefix)

                if prefix in namespace_map:
                    if namespace_map[prefix] != ns_str:
                        # Handle prefix conflicts by creating unique prefixes
                        counter = 1
                        new_prefix = f"{prefix}_{counter}"
                        while new_prefix in namespace_map:
                            counter += 1
                            new_prefix = f"{prefix}_{counter}"
                        namespace_map[new_prefix] = ns_str
                        reverse_map[ns_str].add(new_prefix)
                else:
                    namespace_map[prefix] = ns_str

            merged_graph += graph
        except Exception as e:
            msg = f"Failed to merge {file_path}: {e}"
            raise BuildError(msg) from e

    # Bind prefixes to merged graph, preferring named prefixes over empty prefix
    # For namespaces that have both "" (empty) and a named prefix, skip the empty one
    for prefix, namespace in namespace_map.items():
        prefixes_for_ns = reverse_map.get(namespace, set())
        named_prefixes = [p for p in prefixes_for_ns if p]  # Non-empty prefixes

        # Skip empty prefix if this namespace has a named prefix
        if prefix == "" and named_prefixes:
            logger.debug(
                f"Skipping empty prefix for {namespace}, using named prefix(es): {named_prefixes}"
            )
            continue

        merged_graph.bind(prefix, namespace)

    return merged_graph


def extract_matrix_metadata(graph: Graph) -> tuple[str | None, str | None]:
    """Extract the matrix URI and version from an RDF graph.

    Searches for a subject with type plgt-mtx:Matrix and extracts its URI and version property.

    Args:
        graph: RDF graph containing matrix definition

    Returns:
        Tuple of (matrix_uri, matrix_version), either may be None if not found
    """
    # Define the matrix namespace
    matrix_ns = "https://poliglot.io/os/spec/matrix#"
    rdf_type = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    matrix_version_pred = f"{matrix_ns}version"

    # Query for subjects that are instances of plgt-mtx:Matrix
    matrix_type_uri = f"{matrix_ns}Matrix"
    matrix_uri = None
    matrix_version = None

    for subj, pred, obj in graph:
        if str(pred) == rdf_type and str(obj) == matrix_type_uri:
            matrix_uri = str(subj)
            # Now find the version property for this matrix resource
            for s, p, o in graph:
                if str(s) == matrix_uri and str(p) == matrix_version_pred:
                    matrix_version = str(o)
                    break
            break

    return (matrix_uri, matrix_version)


def create_output_directory(output_dir: Path) -> None:
    """Create the output directory if it doesn't exist.

    Args:
        output_dir: Path to the output directory

    Raises:
        BuildError: If directory creation fails
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        msg = f"Failed to create output directory {output_dir}: {e}"
        raise BuildError(msg) from e


def write_output(graph: Graph, output_path: Path) -> None:
    """Write the merged graph to a Turtle file.

    Args:
        graph: RDF graph to write
        output_path: Path to the output file

    Raises:
        BuildError: If writing fails
    """
    try:
        turtle_data = graph.serialize(format="turtle")
        with output_path.open("w", encoding="utf-8") as f:
            f.write(turtle_data)
    except Exception as e:
        msg = f"Failed to write output to {output_path}: {e}"
        raise BuildError(msg) from e
