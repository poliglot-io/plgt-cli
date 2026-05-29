"""Tests for migration discovery in build_service."""

import tarfile

from plgt.services.archive_service import create_tar_gz_bundle
from plgt.services.build_service import discover_migration_files


class TestDiscoverMigrationFiles:
    def test_no_dir_returns_empty(self, tmp_path):
        assert discover_migration_files(tmp_path) == []

    def test_picks_up_from_files_in_order(self, tmp_path):
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "from-2.1.rq").write_text("# 2.1")
        (migrations / "from-2.0.rq").write_text("# 2.0")
        (migrations / "from-1.0.rq").write_text("# 1.0")

        files = discover_migration_files(tmp_path)
        # Sorted by filename — alphabetical sort of "from-X.Y.rq" is fine for the bundle layout
        # since the engine composes by source version anyway.
        assert [p.name for p in files] == ["from-1.0.rq", "from-2.0.rq", "from-2.1.rq"]

    def test_skips_test_directories(self, tmp_path):
        migrations = tmp_path / "migrations"
        migrations.mkdir()
        (migrations / "from-1.0.rq").write_text("# 1.0")
        test_dir = migrations / "from-1.0.test"
        test_dir.mkdir()
        (test_dir / "input.ttl").write_text("")

        files = discover_migration_files(tmp_path)
        assert [p.name for p in files] == ["from-1.0.rq"]


class TestBundleIncludesMigrations:
    def test_migrations_appear_in_archive(self, tmp_path):
        migrations_src = tmp_path / "src" / "migrations"
        migrations_src.mkdir(parents=True)
        rq = migrations_src / "from-1.0.rq"
        rq.write_text("DELETE WHERE { ?s ?p ?o }")

        output = tmp_path / "matrix.tar.gz"
        create_tar_gz_bundle(
            assembly_content="@prefix ex: <http://x/> .",
            artifacts_dirs=None,
            output_path=output,
            migration_files=[rq],
        )

        with tarfile.open(output, "r:gz") as tar:
            names = tar.getnames()
        assert "assembly.ttl" in names
        assert "migrations/from-1.0.rq" in names
