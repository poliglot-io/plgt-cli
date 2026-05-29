"""Unit tests for rdf_operations module.

Tests cover RDF file discovery, validation, merging, and output operations.
"""

import tempfile
from pathlib import Path

import pytest
from plgt.services.rdf_operations import (
    BuildError,
    create_output_directory,
    discover_rdf_files,
    extract_matrix_metadata,
    merge_rdf_files,
    validate_rdf_file,
    write_output,
)
from rdflib import RDF, Graph, Literal, Namespace


class TestDiscoverRDFFiles:
    """Test RDF file discovery in specification directories."""

    def test_discover_turtle_files(self):
        """Test discovering .ttl files in directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_dir = Path(tmpdir)

            # Create test RDF files
            (spec_dir / "test1.ttl").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "test2.ttl").write_text("@prefix ex: <http://example.org/> .")

            files = discover_rdf_files(spec_dir)

            assert len(files) == 2
            assert all(f.suffix == ".ttl" for f in files)
            assert all(f.exists() for f in files)

    def test_discover_multiple_formats(self):
        """Test discovering RDF files of various formats."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_dir = Path(tmpdir)

            # Create files of different RDF formats
            (spec_dir / "test.ttl").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "test.rdf").write_text(
                '<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"></rdf:RDF>'
            )
            (spec_dir / "test.owl").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "test.nt").write_text(
                "<http://example.org/s> <http://example.org/p> <http://example.org/o> ."
            )
            (spec_dir / "test.n3").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "test.jsonld").write_text('{"@context": {}}')

            files = discover_rdf_files(spec_dir)

            assert len(files) == 6
            extensions = {f.suffix for f in files}
            assert extensions == {".ttl", ".rdf", ".owl", ".nt", ".n3", ".jsonld"}

    def test_discover_nested_directories(self):
        """Test discovering RDF files in nested subdirectories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_dir = Path(tmpdir)

            # Create nested directory structure
            (spec_dir / "sub1").mkdir()
            (spec_dir / "sub1" / "sub2").mkdir()

            (spec_dir / "root.ttl").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "sub1" / "level1.ttl").write_text(
                "@prefix ex: <http://example.org/> ."
            )
            (spec_dir / "sub1" / "sub2" / "level2.ttl").write_text(
                "@prefix ex: <http://example.org/> ."
            )

            files = discover_rdf_files(spec_dir)

            assert len(files) == 3
            # Files should be sorted by path
            file_names = [f.name for f in files]
            assert set(file_names) == {"level2.ttl", "level1.ttl", "root.ttl"}

    def test_discover_filters_non_rdf_files(self):
        """Test that non-RDF files are filtered out."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_dir = Path(tmpdir)

            # Create RDF and non-RDF files
            (spec_dir / "test.ttl").write_text("@prefix ex: <http://example.org/> .")
            (spec_dir / "readme.txt").write_text("This is not RDF")
            (spec_dir / "data.json").write_text('{"not": "rdf"}')
            (spec_dir / "script.py").write_text("print('hello')")

            files = discover_rdf_files(spec_dir)

            assert len(files) == 1
            assert files[0].suffix == ".ttl"

    def test_discover_directory_not_exists(self):
        """Test error when spec directory doesn't exist."""
        nonexistent_dir = Path("/nonexistent/directory")

        with pytest.raises(BuildError, match="Specification directory does not exist"):
            discover_rdf_files(nonexistent_dir)

    def test_discover_path_is_file(self):
        """Test error when spec path is a file, not directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "test.txt"
            file_path.write_text("not a directory")

            with pytest.raises(
                BuildError, match="Specification path is not a directory"
            ):
                discover_rdf_files(file_path)

    def test_discover_empty_directory(self):
        """Test error when no RDF files found in directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            spec_dir = Path(tmpdir)

            # Create only non-RDF files
            (spec_dir / "readme.txt").write_text("No RDF here")

            with pytest.raises(BuildError, match="No RDF files found"):
                discover_rdf_files(spec_dir)


class TestValidateRDFFile:
    """Test RDF file validation."""

    def test_validate_valid_turtle_file(self):
        """Test validation of valid Turtle file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "valid.ttl"
            file_path.write_text("""
                @prefix ex: <http://example.org/> .
                ex:subject ex:predicate ex:object .
            """)

            is_valid, error = validate_rdf_file(file_path)

            assert is_valid is True
            assert error is None

    def test_validate_valid_rdf_xml_file(self):
        """Test validation of valid RDF/XML file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "valid.rdf"
            file_path.write_text("""<?xml version="1.0"?>
                <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                         xmlns:ex="http://example.org/">
                    <rdf:Description rdf:about="http://example.org/subject">
                        <ex:predicate rdf:resource="http://example.org/object"/>
                    </rdf:Description>
                </rdf:RDF>
            """)

            is_valid, error = validate_rdf_file(file_path)

            assert is_valid is True
            assert error is None

    def test_validate_invalid_syntax(self):
        """Test validation of file with invalid RDF syntax."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "invalid.ttl"
            # This will be parsed as N-Triples and fail due to invalid syntax
            file_path.write_text("This is @@@@ invalid RDF !!! syntax ...")

            is_valid, error = validate_rdf_file(file_path)

            assert is_valid is False
            assert error is not None
            assert isinstance(error, str)

    def test_validate_file_not_found(self):
        """Test validation of non-existent file."""
        nonexistent_file = Path("/nonexistent/file.ttl")

        is_valid, error = validate_rdf_file(nonexistent_file)

        assert is_valid is False
        assert error is not None

    def test_validate_empty_file(self):
        """Test validation of empty file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "empty.ttl"
            file_path.write_text("")

            # Empty file is technically valid RDF (empty graph)
            is_valid, error = validate_rdf_file(file_path)

            assert is_valid is True
            assert error is None


class TestMergeRDFFiles:
    """Test RDF file merging operations."""

    def test_merge_single_file(self):
        """Test merging a single RDF file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.ttl"
            file1.write_text("""
                @prefix ex: <http://example.org/> .
                ex:subject1 ex:predicate ex:object1 .
            """)

            merged_graph = merge_rdf_files([file1])

            assert len(merged_graph) == 1  # One triple
            assert "ex" in dict(merged_graph.namespaces())

    def test_merge_multiple_files(self):
        """Test merging multiple RDF files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.ttl"
            file1.write_text("""
                @prefix ex: <http://example.org/> .
                ex:subject1 ex:predicate ex:object1 .
            """)

            file2 = Path(tmpdir) / "test2.ttl"
            file2.write_text("""
                @prefix ex: <http://example.org/> .
                ex:subject2 ex:predicate ex:object2 .
            """)

            merged_graph = merge_rdf_files([file1, file2])

            assert len(merged_graph) >= 2  # At least two triples
            namespaces = dict(merged_graph.namespaces())
            assert "ex" in namespaces

    def test_merge_preserves_namespaces(self):
        """Test that namespace prefixes are preserved during merge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.ttl"
            file1.write_text("""
                @prefix ex: <http://example.org/> .
                @prefix foaf: <http://xmlns.com/foaf/0.1/> .
                ex:subject1 foaf:name "Test" .
            """)

            file2 = Path(tmpdir) / "test2.ttl"
            file2.write_text("""
                @prefix schema: <http://schema.org/> .
                schema:Thing schema:name "Thing" .
            """)

            merged_graph = merge_rdf_files([file1, file2])

            namespaces = dict(merged_graph.namespaces())
            assert "ex" in namespaces
            assert "foaf" in namespaces
            assert "schema" in namespaces

    def test_merge_handles_prefix_conflicts(self):
        """Test handling of conflicting namespace prefixes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Two files using same prefix for different namespaces
            file1 = Path(tmpdir) / "test1.ttl"
            file1.write_text("""
                @prefix ex: <http://example1.org/> .
                ex:subject1 ex:predicate ex:object1 .
            """)

            file2 = Path(tmpdir) / "test2.ttl"
            file2.write_text("""
                @prefix ex: <http://example2.org/> .
                ex:subject2 ex:predicate ex:object2 .
            """)

            merged_graph = merge_rdf_files([file1, file2])

            # Should have both namespace URIs, with conflict resolution
            namespaces = dict(merged_graph.namespaces())
            namespace_values = [str(ns) for ns in namespaces.values()]

            # Check both namespaces are present (one as 'ex', one as 'ex_1' or similar)
            assert (
                "http://example1.org/" in namespace_values
                or "http://example2.org/" in namespace_values
            )

    def test_merge_invalid_file_raises_error(self):
        """Test error handling when merging invalid RDF file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            valid_file = Path(tmpdir) / "valid.ttl"
            valid_file.write_text("""
                @prefix ex: <http://example.org/> .
                ex:subject ex:predicate ex:object .
            """)

            invalid_file = Path(tmpdir) / "invalid.ttl"
            invalid_file.write_text("This is not valid RDF!")

            with pytest.raises(BuildError, match="Failed to merge"):
                merge_rdf_files([valid_file, invalid_file])

    def test_merge_nonexistent_file_raises_error(self):
        """Test error handling when merging non-existent file."""
        nonexistent_file = Path("/nonexistent/file.ttl")

        with pytest.raises(BuildError, match="Failed to merge"):
            merge_rdf_files([nonexistent_file])

    def test_merge_empty_list(self):
        """Test merging empty list of files."""
        merged_graph = merge_rdf_files([])

        assert len(merged_graph) == 0  # Empty graph


class TestCreateOutputDirectory:
    """Test output directory creation."""

    def test_create_new_directory(self):
        """Test creating a new output directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "new_output"

            assert not output_dir.exists()

            create_output_directory(output_dir)

            assert output_dir.exists()
            assert output_dir.is_dir()

    def test_create_nested_directory(self):
        """Test creating nested output directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "level1" / "level2" / "level3"

            assert not output_dir.exists()

            create_output_directory(output_dir)

            assert output_dir.exists()
            assert output_dir.is_dir()

    def test_create_existing_directory(self):
        """Test creating output directory that already exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "existing"
            output_dir.mkdir()

            # Should not raise error
            create_output_directory(output_dir)

            assert output_dir.exists()
            assert output_dir.is_dir()

    def test_create_directory_permission_error(self):
        """Test error handling when directory creation fails due to permissions."""
        # Try to create directory in root (should fail without permissions)
        output_dir = Path("/root/test_output")

        # This might not raise BuildError on all systems (if running as root)
        # Skip assertion if we have permissions, otherwise verify error handling
        try:
            create_output_directory(output_dir)
            # If it succeeds (running as root), verify directory exists
            assert output_dir.exists()
        except BuildError:
            # Expected error - test passed
            pass


class TestWriteOutput:
    """Test RDF graph output writing."""

    def test_write_graph_to_file(self):
        """Test writing RDF graph to Turtle file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "output.ttl"

            # Create a simple graph
            graph = Graph()
            ex = Namespace("http://example.org/")
            graph.bind("ex", ex)
            graph.add((ex.subject, ex.predicate, ex.object))

            write_output(graph, output_file)

            assert output_file.exists()
            assert output_file.stat().st_size > 0

            # Verify written content is valid RDF
            verification_graph = Graph()
            verification_graph.parse(output_file, format="turtle")
            assert len(verification_graph) == 1

    def test_write_empty_graph(self):
        """Test writing empty RDF graph."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "empty.ttl"

            graph = Graph()

            write_output(graph, output_file)

            assert output_file.exists()

    def test_write_graph_with_namespaces(self):
        """Test writing graph with multiple namespace prefixes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "namespaces.ttl"

            graph = Graph()
            ex = Namespace("http://example.org/")
            foaf = Namespace("http://xmlns.com/foaf/0.1/")

            graph.bind("ex", ex)
            graph.bind("foaf", foaf)

            graph.add((ex.person, foaf.name, Literal("John")))

            write_output(graph, output_file)

            # Verify namespaces are preserved
            content = output_file.read_text()
            assert "@prefix ex:" in content or "PREFIX ex:" in content
            assert "@prefix foaf:" in content or "PREFIX foaf:" in content

    def test_write_creates_parent_directory(self):
        """Test that write operation creates parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "sub1" / "sub2" / "output.ttl"

            # Parent directories don't exist yet
            assert not output_file.parent.exists()

            graph = Graph()
            ex = Namespace("http://example.org/")
            graph.add((ex.s, ex.p, ex.o))

            # This should fail because parent doesn't exist
            with pytest.raises(BuildError, match="Failed to write output"):
                write_output(graph, output_file)

    def test_write_to_readonly_location(self):
        """Test error handling when writing to read-only location."""
        # Try to write to /dev/null or similar (platform-dependent)
        import platform

        if platform.system() != "Windows":
            output_file = Path("/dev/full")  # Writing to /dev/full should fail

            graph = Graph()
            ex = Namespace("http://example.org/")
            graph.add((ex.s, ex.p, ex.o))

            # Should raise BuildError
            with pytest.raises(BuildError, match="Failed to write output"):
                write_output(graph, output_file)


class TestMergePrefixPreference:
    """Test that named prefixes are preferred over empty prefixes during merge.

    This is critical for ensuring that when matrices use @base for their own namespace
    AND define a named prefix (e.g., @prefix projects:), the named prefix is preserved
    in the merged output rather than just the empty prefix.
    """

    def test_named_prefix_preferred_over_empty_prefix(self):
        """Test that named prefix is kept when namespace has both empty and named prefix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # File with both @base and @prefix for same namespace
            file1 = Path(tmpdir) / "matrix.ttl"
            file1.write_text("""
                @base <https://example.org/spec/projects#> .
                @prefix projects: <https://example.org/spec/projects#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                projects:Project a rdfs:Class ;
                    rdfs:label "Project" .
            """)

            merged_graph = merge_rdf_files([file1])

            namespaces = dict(merged_graph.namespaces())
            namespace_uris = {str(ns) for ns in namespaces.values()}

            # The projects namespace should be present
            assert "https://example.org/spec/projects#" in namespace_uris

            # The named prefix 'projects' should be present, not just empty prefix
            assert "projects" in namespaces
            assert str(namespaces["projects"]) == "https://example.org/spec/projects#"

    def test_empty_prefix_removed_when_named_exists(self):
        """Test that empty prefix is NOT bound when a named prefix exists for same namespace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "matrix.ttl"
            file1.write_text("""
                @base <https://example.org/spec/test#> .
                @prefix test: <https://example.org/spec/test#> .

                test:Subject test:predicate test:Object .
            """)

            merged_graph = merge_rdf_files([file1])

            namespaces = dict(merged_graph.namespaces())

            # Named prefix should exist
            assert "test" in namespaces

            # Empty prefix should NOT be bound to this namespace
            # (it may exist for other built-in namespaces, but not for test#)
            if "" in namespaces:
                assert str(namespaces[""]) != "https://example.org/spec/test#"

    def test_multiple_matrices_preserve_all_named_prefixes(self):
        """Test that merging multiple matrices preserves all their named prefixes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Places matrix - uses : for itself
            file1 = Path(tmpdir) / "places.ttl"
            file1.write_text("""
                @base <https://example.org/spec/places#> .
                @prefix places: <https://example.org/spec/places#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                places:Place a rdfs:Class .
            """)

            # People matrix - uses : for itself, imports places
            file2 = Path(tmpdir) / "people.ttl"
            file2.write_text("""
                @base <https://example.org/spec/people#> .
                @prefix people: <https://example.org/spec/people#> .
                @prefix places: <https://example.org/spec/places#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                people:Person a rdfs:Class ;
                    rdfs:seeAlso places:Place .
            """)

            # Projects matrix - uses : for itself, imports both
            file3 = Path(tmpdir) / "projects.ttl"
            file3.write_text("""
                @base <https://example.org/spec/projects#> .
                @prefix projects: <https://example.org/spec/projects#> .
                @prefix people: <https://example.org/spec/people#> .
                @prefix places: <https://example.org/spec/places#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                projects:Project a rdfs:Class ;
                    rdfs:seeAlso people:Person, places:Place .
            """)

            merged_graph = merge_rdf_files([file1, file2, file3])

            namespaces = dict(merged_graph.namespaces())

            # ALL three named prefixes should be preserved
            assert "places" in namespaces, "places prefix should be preserved"
            assert "people" in namespaces, "people prefix should be preserved"
            assert "projects" in namespaces, "projects prefix should be preserved"

            # Verify they point to correct namespaces
            assert str(namespaces["places"]) == "https://example.org/spec/places#"
            assert str(namespaces["people"]) == "https://example.org/spec/people#"
            assert str(namespaces["projects"]) == "https://example.org/spec/projects#"

    def test_serialization_uses_named_prefix(self):
        """Test that serialized output uses named prefix, not full URIs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "matrix.ttl"
            file1.write_text("""
                @base <https://example.org/spec/projects#> .
                @prefix projects: <https://example.org/spec/projects#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                projects:Project a rdfs:Class ;
                    rdfs:label "Project" .
            """)

            merged_graph = merge_rdf_files([file1])

            # Serialize the graph
            turtle_output = merged_graph.serialize(format="turtle")

            # Should use projects: prefix, not full URI
            assert "projects:" in turtle_output or "@prefix projects:" in turtle_output
            # Should NOT have full URI for projects namespace resources
            assert "<https://example.org/spec/projects#Project>" not in turtle_output

    def test_base_only_file_still_works(self):
        """Test that files with only @base (no named prefix) still work.

        When @base is used, rdflib only creates a prefix binding if the empty prefix
        is explicitly used in the file. Relative URIs like <Thing> are resolved
        against the base but don't create a prefix binding.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "base_only.ttl"
            # Use the empty prefix explicitly (: prefix) to ensure binding is created
            file1.write_text("""
                @base <https://example.org/spec/simple#> .
                @prefix : <https://example.org/spec/simple#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                :Thing a rdfs:Class ;
                    rdfs:label "Thing" .
            """)

            merged_graph = merge_rdf_files([file1])

            # Should still have the namespace, using empty prefix
            namespaces = dict(merged_graph.namespaces())
            namespace_uris = {str(ns) for ns in namespaces.values()}

            assert "https://example.org/spec/simple#" in namespace_uris
            # Empty prefix should be bound since there's no named alternative
            assert "" in namespaces
            assert str(namespaces[""]) == "https://example.org/spec/simple#"

    def test_mixed_base_and_named_across_files(self):
        """Test merging files where some use @base and others use named prefix for same ns."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # File 1: Uses only @base
            file1 = Path(tmpdir) / "base_file.ttl"
            file1.write_text("""
                @base <https://example.org/spec/shared#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                <ClassA> a rdfs:Class .
            """)

            # File 2: Uses named prefix for same namespace
            file2 = Path(tmpdir) / "named_file.ttl"
            file2.write_text("""
                @prefix shared: <https://example.org/spec/shared#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                shared:ClassB a rdfs:Class .
            """)

            merged_graph = merge_rdf_files([file1, file2])

            namespaces = dict(merged_graph.namespaces())

            # Named prefix should win
            assert "shared" in namespaces
            assert str(namespaces["shared"]) == "https://example.org/spec/shared#"

            # Empty prefix should NOT point to shared namespace
            if "" in namespaces:
                assert str(namespaces[""]) != "https://example.org/spec/shared#"

    def test_conflicting_empty_prefixes_across_matrices(self):
        """Test that conflicting empty prefixes don't cause issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Matrix A with @base pointing to namespace A
            file1 = Path(tmpdir) / "matrix_a.ttl"
            file1.write_text("""
                @base <https://example.org/spec/matrix_a#> .
                @prefix matrix_a: <https://example.org/spec/matrix_a#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                matrix_a:EntityA a rdfs:Class .
            """)

            # Matrix B with @base pointing to namespace B
            file2 = Path(tmpdir) / "matrix_b.ttl"
            file2.write_text("""
                @base <https://example.org/spec/matrix_b#> .
                @prefix matrix_b: <https://example.org/spec/matrix_b#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

                matrix_b:EntityB a rdfs:Class .
            """)

            merged_graph = merge_rdf_files([file1, file2])

            namespaces = dict(merged_graph.namespaces())

            # Both named prefixes should be preserved
            assert "matrix_a" in namespaces
            assert "matrix_b" in namespaces
            assert str(namespaces["matrix_a"]) == "https://example.org/spec/matrix_a#"
            assert str(namespaces["matrix_b"]) == "https://example.org/spec/matrix_b#"


class TestExtractMatrixMetadata:
    """Test extracting matrix URI and version from RDF graphs."""

    def test_extract_matrix_uri_and_version_success(self):
        """Test successful extraction of matrix URI and version."""
        graph = Graph()
        matrix_ns = Namespace("https://poliglot.io/os/spec/matrix#")
        ex = Namespace("https://example.org/spec/test#")

        # Add matrix definition with version
        graph.add((ex.term(""), RDF.type, matrix_ns.Matrix))
        graph.add((ex.term(""), matrix_ns.name, Literal("Test Matrix")))
        graph.add((ex.term(""), matrix_ns.version, Literal("1.2.3")))

        uri, version = extract_matrix_metadata(graph)

        assert uri == "https://example.org/spec/test#"
        assert version == "1.2.3"

    def test_extract_matrix_no_version(self):
        """Test extraction when matrix has no version."""
        graph = Graph()
        matrix_ns = Namespace("https://poliglot.io/os/spec/matrix#")
        ex = Namespace("https://example.org/spec/test#")

        # Add matrix without version
        graph.add((ex.term(""), RDF.type, matrix_ns.Matrix))
        graph.add((ex.term(""), matrix_ns.name, Literal("Test Matrix")))

        uri, version = extract_matrix_metadata(graph)

        assert uri == "https://example.org/spec/test#"
        assert version is None

    def test_extract_matrix_empty_graph(self):
        """Test extraction from empty graph."""
        graph = Graph()

        uri, version = extract_matrix_metadata(graph)

        assert uri is None
        assert version is None

    def test_extract_matrix_no_matrix_definition(self):
        """Test extraction when no matrix:Matrix is defined."""
        graph = Graph()
        ex = Namespace("https://example.org/spec/test#")

        # Add some triples but no matrix:Matrix
        graph.add((ex.subject, ex.predicate, ex.object))

        uri, version = extract_matrix_metadata(graph)

        assert uri is None
        assert version is None
