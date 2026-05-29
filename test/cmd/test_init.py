"""Unit tests for the init command.

Tests cover poliglot.yml and .gitignore generation from the bundled templates.
"""

from plgt.services.template_service import render_template


class TestPoliglotYmlTemplate:
    """Template renders the expected fields for a minimal project."""

    def test_renders_with_minimal_inputs(self):
        result = render_template(
            "poliglot.yml.j2",
            name="tasks",
            version="0.1.0",
            engine_version=">=1 <2",
            spec_patterns=["./spec"],
            artifact_patterns=["./spec/artifacts"],
            components=None,
            output_dir="./.matrix",
        )
        assert 'name: "tasks"' in result
        assert 'version: "0.1.0"' in result
        assert 'engineVersion: ">=1 <2"' in result
        assert "- ./spec" in result
        assert "- ./spec/artifacts" in result
        assert "outputDir: ./.matrix" in result
        # components section is omitted when not provided
        assert "components:" not in result

    def test_renders_components_section_when_provided(self):
        result = render_template(
            "poliglot.yml.j2",
            name="tasks",
            version="0.1.0",
            engine_version=">=1 <2",
            spec_patterns=["./spec"],
            artifact_patterns=["./spec/artifacts"],
            components={"source": "./src/components", "entry": "index.ts"},
            output_dir="./.matrix",
        )
        assert "components:" in result
        assert "source: ./src/components" in result
        assert "entry: index.ts" in result


class TestGitignoreTemplate:
    """Template renders the output directory entry."""

    def test_renders_output_dir(self):
        result = render_template("gitignore.j2", output_dir="./.matrix")
        assert ".matrix" in result
