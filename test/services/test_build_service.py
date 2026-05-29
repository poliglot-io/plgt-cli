"""Unit tests for build_service module.

Tests cover validation error reporting during matrix builds.
"""

import tempfile
from pathlib import Path

import pytest
from plgt.models.build_types import MatrixBuildConfig
from plgt.services.build_progress import create_progress_tracker
from plgt.services.rdf_operations import BuildError
from rich.console import Console


class TestBuildMatrixValidation:
    """Test that build_matrix reports validation errors with file details."""

    def _make_config(self, matrix_dir: Path) -> MatrixBuildConfig:
        return MatrixBuildConfig(
            name="test-matrix",
            path=Path(),
            spec_patterns=["./spec"],
            artifact_patterns=[],
            output_dir=Path("dist"),
            components=None,
        )

    def test_invalid_files_raises_with_error_details(self):
        """BuildError must include each invalid file path and its error message."""
        from plgt.services.build_service import build_matrix

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            spec_dir = project_dir / "spec"
            spec_dir.mkdir()

            # One valid file so we pass the "no valid files" check on the first
            # validation pass, then add an invalid file that will be caught by
            # the final validation.
            valid = spec_dir / "valid.ttl"
            valid.write_text(
                "@prefix ex: <http://example.org/> .\nex:Thing a ex:Class .\n"
            )

            invalid = spec_dir / "broken.ttl"
            invalid.write_text("this is not valid turtle at all {{{")

            config = self._make_config(project_dir)
            console = Console()
            progress = create_progress_tracker(console)

            with progress, pytest.raises(BuildError, match=r"broken\.ttl") as exc_info:
                build_matrix(progress, config, project_dir)

            error_msg = str(exc_info.value)
            assert "1 invalid RDF file" in error_msg
            assert "broken.ttl" in error_msg

    def test_multiple_invalid_files_all_listed(self):
        """All invalid files and their errors must appear in the BuildError."""
        from plgt.services.build_service import build_matrix

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            spec_dir = project_dir / "spec"
            spec_dir.mkdir()

            valid = spec_dir / "valid.ttl"
            valid.write_text(
                "@prefix ex: <http://example.org/> .\nex:Thing a ex:Class .\n"
            )

            bad1 = spec_dir / "bad1.ttl"
            bad1.write_text("not valid {{{")

            bad2 = spec_dir / "bad2.ttl"
            bad2.write_text("also broken |||")

            config = self._make_config(project_dir)
            console = Console()
            progress = create_progress_tracker(console)

            with progress, pytest.raises(BuildError) as exc_info:
                build_matrix(progress, config, project_dir)

            error_msg = str(exc_info.value)
            assert "2 invalid RDF files" in error_msg
            assert "bad1.ttl" in error_msg
            assert "bad2.ttl" in error_msg

    def test_no_valid_files_raises_build_error(self):
        """When all files are invalid, BuildError should mention no valid files."""
        from plgt.services.build_service import build_matrix

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            spec_dir = project_dir / "spec"
            spec_dir.mkdir()

            bad = spec_dir / "bad.ttl"
            bad.write_text("completely invalid")

            config = self._make_config(project_dir)
            console = Console()
            progress = create_progress_tracker(console)

            with progress, pytest.raises(BuildError, match="No valid RDF files"):
                build_matrix(progress, config, project_dir)

    def test_all_valid_files_no_error(self):
        """When all files are valid, no BuildError should be raised."""
        from plgt.services.build_service import build_matrix

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            spec_dir = project_dir / "spec"
            spec_dir.mkdir()
            (project_dir / "dist").mkdir()

            valid = spec_dir / "valid.ttl"
            valid.write_text(
                "@prefix ex: <http://example.org/> .\n"
                "@prefix matrix: <https://poliglot.io/os/spec/matrix#> .\n"
                "ex:TestMatrix a matrix:Matrix .\n"
            )

            config = self._make_config(project_dir)
            console = Console()
            progress = create_progress_tracker(console)

            # Should not raise — we don't care about the result details here,
            # just that no BuildError is thrown during validation.
            with progress:
                result = build_matrix(progress, config, project_dir)

            assert result.valid_files_count == 1
            assert len(result.invalid_files) == 0


class TestEngineVersionRangeValidation:
    """Build-time validation of engineVersion as a major.minor-only range."""

    def _write_config(self, project_dir: Path, engine_version: str) -> Path:
        config_path = project_dir / "poliglot.yml"
        config_path.write_text(
            "package:\n"
            "  name: demo\n"
            "  version: 0.0.1\n"
            f'  engineVersion: "{engine_version}"\n'
            "matrix:\n"
            "  demo:\n"
            "    path: ./demo\n"
            "    spec:\n"
            "      - ./spec\n"
        )
        return config_path

    def test_accepts_two_component_range(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, ">=2.1 <3.0")

            cfg = create_build_config(config_path)

            assert cfg.engine_version == ">=2.1 <3.0"

    def test_accepts_bare_major_range(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, ">=2 <4")

            cfg = create_build_config(config_path)

            assert cfg.engine_version == ">=2 <4"

    def test_rejects_three_component_range_with_canonical_message(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, ">=2.1.3 <3.0")

            with pytest.raises(BuildError) as exc_info:
                create_build_config(config_path)

            msg = str(exc_info.value)
            assert "major.minor only" in msg
            assert ">=2.1 <3.0" in msg
            assert "Patch versions are not allowed" in msg

    def test_rejects_bare_version_without_operator(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "1")

            with pytest.raises(BuildError) as exc_info:
                create_build_config(config_path)

            assert "Invalid engineVersion range" in str(exc_info.value)

    def test_rejects_unsupported_operator(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "^2.1")

            with pytest.raises(BuildError):
                create_build_config(config_path)


class TestPackageNameValidation:
    """Build-time enforcement of the registry naming pattern on `package.name`."""

    def _write_config(
        self,
        project_dir: Path,
        name: str,
        publisher: str | None = None,
    ) -> Path:
        config_path = project_dir / "poliglot.yml"
        publisher_line = f'  publisher: "{publisher}"\n' if publisher else ""
        config_path.write_text(
            "package:\n"
            f'  name: "{name}"\n'
            f"{publisher_line}"
            "  version: 0.0.1\n"
            '  engineVersion: ">=2 <3"\n'
            "matrix:\n"
            "  demo:\n"
            "    path: ./demo\n"
            "    spec:\n"
            "      - ./spec\n"
        )
        return config_path

    def test_accepts_valid_name(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "my-package")

            cfg = create_build_config(config_path)

            assert cfg.name == "my-package"

    def test_rejects_uppercase_name(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "BadName")

            with pytest.raises(BuildError, match=r"package\.name"):
                create_build_config(config_path)

    def test_rejects_name_with_slash(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "ev/il")

            with pytest.raises(BuildError, match=r"package\.name"):
                create_build_config(config_path)

    def test_rejects_name_with_underscore(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(project_dir, "with_underscore")

            with pytest.raises(BuildError, match=r"package\.name"):
                create_build_config(config_path)

    def test_publisher_field_is_silently_ignored(self):
        """``publisher`` in poliglot.yml has no effect — publisher identity
        is decided by CI/auth at publish time, not by the local config.
        Build succeeds regardless of what (if anything) the user wrote.
        """
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config(
                project_dir, "okay-name", publisher="anything-here"
            )

            cfg = create_build_config(config_path)

            assert cfg.name == "okay-name"


class TestPackageDependenciesParsing:
    """Parsing and validation of cross-package deps in ``poliglot.yml``. Mirrors the
    engine-version range tests above but for the ``dependencies`` map.
    """

    def _write_config_with_deps(
        self,
        project_dir: Path,
        deps_yaml: str | None,
        publisher: str | None = None,
    ) -> Path:
        config_path = project_dir / "poliglot.yml"
        publisher_line = f"  publisher: {publisher}\n" if publisher else ""
        deps_block = "" if deps_yaml is None else f"\ndependencies:\n{deps_yaml}\n"
        config_path.write_text(
            "package:\n"
            f"  name: demo\n"
            f"{publisher_line}"
            "  version: 0.0.1\n"
            '  engineVersion: ">=2 <3"\n'
            f"{deps_block}"
            "matrix:\n"
            "  demo:\n"
            "    path: ./demo\n"
            "    spec:\n"
            "      - ./spec\n"
        )
        return config_path

    def test_no_dependencies_field_yields_empty_list(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(project_dir, None)

            cfg = create_build_config(config_path)

            assert cfg.dependencies == []

    def test_valid_dependencies_parsed_in_order(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(
                project_dir,
                '  "widget/core": ">=1 <2"\n  "auth/base": ">=2.1 <3"',
            )

            cfg = create_build_config(config_path)

            assert len(cfg.dependencies) == 2
            assert cfg.dependencies[0].publisher == "widget"
            assert cfg.dependencies[0].name == "core"
            assert cfg.dependencies[0].version_range == ">=1 <2"
            assert cfg.dependencies[1].publisher == "auth"
            assert cfg.dependencies[1].name == "base"
            assert cfg.dependencies[1].version_range == ">=2.1 <3"

    def test_rejects_malformed_publisher_name_key(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(
                project_dir, '  "no-slash-key": ">=1 <2"'
            )

            with pytest.raises(BuildError, match="publisher/name"):
                create_build_config(config_path)

    def test_rejects_three_component_dep_range(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(
                project_dir, '  "widget/core": ">=1.2.3 <2"'
            )

            with pytest.raises(BuildError, match="Invalid dependency range"):
                create_build_config(config_path)

    def test_self_reference_check_deferred_to_publish_time(self):
        """The build path no longer rejects self-references — the local
        project doesn't authoritatively know its own publisher slug, so the
        check moves to publish time where CI/auth supplies the identity.
        """
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(
                project_dir,
                '  "acme/demo": ">=1 <2"',
                publisher="acme",
            )

            # Even with publisher: acme + a dep on acme/demo (the package's
            # own name), the build accepts and records the dep verbatim.
            cfg = create_build_config(config_path)

            assert any(
                d.publisher == "acme" and d.name == "demo" for d in cfg.dependencies
            )

    def test_rejects_non_string_version_range(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            config_path = self._write_config_with_deps(
                project_dir, '  "widget/core": 42'
            )

            with pytest.raises(BuildError, match="must be a string"):
                create_build_config(config_path)


class TestPackageDependenciesInArchive:
    """Verify the manifest written into the package archive includes the declared dependencies
    in the documented JSON shape."""

    def test_manifest_includes_dependencies_when_declared(self):
        import json
        import tarfile
        from io import BytesIO

        from plgt.models.build_types import (
            MatrixBuildResult,
            PackageConfig,
            PackageDependency,
        )
        from plgt.services.archive_service import create_package_archive

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            output_path = project_dir / "package.tgz"

            # Pre-build a synthetic matrix bundle so the archive can copy assembly.ttl out of
            # it. The bundle contents themselves are not under test here.
            matrix_dir = project_dir / "demo"
            matrix_dir.mkdir()
            bundle_path = matrix_dir / "matrix.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as tar:
                content = b"# placeholder\n"
                info = tarfile.TarInfo(name="assembly.ttl")
                info.size = len(content)
                tar.addfile(info, BytesIO(content))

            cfg = PackageConfig(
                name="demo",
                version="0.0.1",
                engine_version=">=2 <3",
                project_dir=project_dir,
                matrices=[],
                dependencies=[
                    PackageDependency(
                        publisher="widget", name="core", version_range=">=1 <2"
                    ),
                    PackageDependency(
                        publisher="auth", name="base", version_range=">=2.1 <3"
                    ),
                ],
            )
            results = [
                MatrixBuildResult(
                    name="demo",
                    output_dir=matrix_dir,
                    matrix_uri="https://example.com/m",
                    total_triples=0,
                    valid_files_count=0,
                    total_files_count=0,
                    invalid_files=[],
                )
            ]

            create_package_archive(cfg, results, output_path)

            with tarfile.open(output_path, "r:gz") as tar:
                manifest_member = tar.getmember("manifest.json")
                f = tar.extractfile(manifest_member)
                assert f is not None
                manifest = json.loads(f.read().decode("utf-8"))

            assert "dependencies" in manifest
            assert manifest["dependencies"] == [
                {
                    "publisher": "widget",
                    "name": "core",
                    "versionRange": ">=1 <2",
                },
                {
                    "publisher": "auth",
                    "name": "base",
                    "versionRange": ">=2.1 <3",
                },
            ]

    def test_manifest_omits_dependencies_when_none_declared(self):
        import json
        import tarfile
        from io import BytesIO

        from plgt.models.build_types import MatrixBuildResult, PackageConfig
        from plgt.services.archive_service import create_package_archive

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir)
            output_path = project_dir / "package.tgz"

            matrix_dir = project_dir / "demo"
            matrix_dir.mkdir()
            bundle_path = matrix_dir / "matrix.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as tar:
                content = b"# placeholder\n"
                info = tarfile.TarInfo(name="assembly.ttl")
                info.size = len(content)
                tar.addfile(info, BytesIO(content))

            cfg = PackageConfig(
                name="demo",
                version="0.0.1",
                engine_version=">=2 <3",
                project_dir=project_dir,
                matrices=[],
            )
            results = [
                MatrixBuildResult(
                    name="demo",
                    output_dir=matrix_dir,
                    matrix_uri="https://example.com/m",
                    total_triples=0,
                    valid_files_count=0,
                    total_files_count=0,
                    invalid_files=[],
                )
            ]

            create_package_archive(cfg, results, output_path)

            with tarfile.open(output_path, "r:gz") as tar:
                manifest_member = tar.getmember("manifest.json")
                f = tar.extractfile(manifest_member)
                assert f is not None
                manifest = json.loads(f.read().decode("utf-8"))

            assert "dependencies" not in manifest


class TestPackageMetadataParsing:
    """poliglot.yml's optional package metadata fields (description, repositoryUrl, homepage,
    license, changelog, tags) flow into the parsed PackageConfig and then into the emitted
    manifest.json."""

    def _write_config(self, project_dir: Path, package_extras: str = "") -> Path:
        config_path = project_dir / "poliglot.yml"
        config_path.write_text(
            "package:\n"
            '  name: "demo"\n'
            "  version: 0.0.1\n"
            '  engineVersion: ">=2 <3"\n'
            f"{package_extras}"
            "matrix:\n"
            "  demo:\n"
            "    path: ./demo\n"
            "    spec:\n"
            "      - ./spec\n"
        )
        return config_path

    def test_metadata_fields_default_to_none_or_empty(self):
        from plgt.services.build_service import create_build_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = create_build_config(self._write_config(Path(tmpdir)))

            assert cfg.description is None
            assert cfg.repository_url is None
            assert cfg.homepage is None
            assert cfg.license is None
            assert cfg.changelog is None
            assert cfg.tags == []

    def test_metadata_fields_parsed_from_yaml(self):
        from plgt.services.build_service import create_build_config

        extras = (
            '  description: "Widget service integration"\n'
            '  repositoryUrl: "https://github.com/acme/crm"\n'
            '  homepage: "https://acme.example/crm"\n'
            '  license: "Apache-2.0"\n'
            '  changelog: "First release"\n'
            "  tags:\n"
            '    - "crm"\n'
            '    - "ai-providers"\n'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = create_build_config(self._write_config(Path(tmpdir), extras))

            assert cfg.description == "Widget service integration"
            assert cfg.repository_url == "https://github.com/acme/crm"
            assert cfg.homepage == "https://acme.example/crm"
            assert cfg.license == "Apache-2.0"
            assert cfg.changelog == "First release"
            assert cfg.tags == ["crm", "ai-providers"]

    def test_blank_string_fields_normalize_to_none(self):
        """A blank or whitespace-only string in YAML is indistinguishable from "absent" at the
        publish boundary — both yield missing manifest keys. Normalize at parse time so the
        archive emitter doesn't have to defensively .strip() each field."""
        from plgt.services.build_service import create_build_config

        extras = '  description: "   "\n  license: ""\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = create_build_config(self._write_config(Path(tmpdir), extras))

            assert cfg.description is None
            assert cfg.license is None

    def test_rejects_non_list_tags(self):
        from plgt.services.build_service import create_build_config

        extras = '  tags: "not-a-list"\n'
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(BuildError, match=r"tags must be a list"):
                create_build_config(self._write_config(Path(tmpdir), extras))

    def test_rejects_non_string_tag_entries(self):
        from plgt.services.build_service import create_build_config

        extras = "  tags:\n    - valid\n    - 123\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(BuildError, match=r"tags must be a list of strings"):
                create_build_config(self._write_config(Path(tmpdir), extras))


class TestPackageMetadataInArchive:
    """Verify the manifest written into the package archive includes optional metadata fields
    when declared, and omits them otherwise so the manifest shape stays minimal."""

    def _build_archive(self, project_dir: Path, **package_kwargs) -> dict:
        import json
        import tarfile
        from io import BytesIO

        from plgt.models.build_types import MatrixBuildResult, PackageConfig
        from plgt.services.archive_service import create_package_archive

        output_path = project_dir / "package.tgz"
        matrix_dir = project_dir / "demo"
        matrix_dir.mkdir(exist_ok=True)
        bundle_path = matrix_dir / "matrix.tar.gz"
        with tarfile.open(bundle_path, "w:gz") as tar:
            content = b"# placeholder\n"
            info = tarfile.TarInfo(name="assembly.ttl")
            info.size = len(content)
            tar.addfile(info, BytesIO(content))

        cfg = PackageConfig(
            name="demo",
            version="0.0.1",
            engine_version=">=2 <3",
            project_dir=project_dir,
            matrices=[],
            **package_kwargs,
        )
        results = [
            MatrixBuildResult(
                name="demo",
                output_dir=matrix_dir,
                matrix_uri="https://example.com/m",
                total_triples=0,
                valid_files_count=0,
                total_files_count=0,
                invalid_files=[],
            )
        ]
        create_package_archive(cfg, results, output_path)

        with tarfile.open(output_path, "r:gz") as tar:
            manifest_member = tar.getmember("manifest.json")
            f = tar.extractfile(manifest_member)
            assert f is not None
            return json.loads(f.read().decode("utf-8"))

    def test_manifest_includes_metadata_when_declared(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = self._build_archive(
                Path(tmpdir),
                description="Demo package",
                repository_url="https://github.com/acme/demo",
                homepage="https://acme.example",
                license="MIT",
                changelog="First release",
                tags=["ai-providers", "demo"],
            )

            assert manifest["description"] == "Demo package"
            assert manifest["repositoryUrl"] == "https://github.com/acme/demo"
            assert manifest["homepage"] == "https://acme.example"
            assert manifest["license"] == "MIT"
            assert manifest["changelog"] == "First release"
            assert manifest["tags"] == ["ai-providers", "demo"]

    def test_manifest_omits_metadata_when_not_declared(self):
        """Manifest keys are omitted entirely (not emitted as null) when the publisher didn't
        declare them. Server-side parser distinguishes absent from blank, and CLI shouldn't
        emit blanks the parser would have to handle separately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = self._build_archive(Path(tmpdir))

            for key in (
                "description",
                "repositoryUrl",
                "homepage",
                "license",
                "changelog",
                "tags",
            ):
                assert key not in manifest, f"unexpected key {key} in minimal manifest"
