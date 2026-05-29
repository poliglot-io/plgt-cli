"""Unit tests for ``plgt.utils.workspace_mode``."""

from __future__ import annotations

import pytest
from plgt.core.exceptions import ValidationError
from plgt.utils.workspace_mode import resolve_workspace_mode


class _FakeConfig:
    """Minimal stand-in for ``plgt.core.config`` exposing only ``.defaults``,
    which is what ``resolve_workspace_mode`` reads.
    """

    def __init__(self, defaults: dict[str, str]) -> None:
        self.defaults = defaults


class TestResolveWorkspaceMode:
    def test_explicit_from_workspace_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if a default is configured, --from-workspace explicit wins.
        monkeypatch.setattr(
            "plgt.utils.workspace_mode.config", _FakeConfig({"workspace": "dev"})
        )
        assert (
            resolve_workspace_mode(from_workspace="prod", from_registry=False) == "prod"
        )

    def test_explicit_from_registry_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "plgt.utils.workspace_mode.config", _FakeConfig({"workspace": "dev"})
        )
        assert resolve_workspace_mode(from_workspace=None, from_registry=True) is None

    def test_default_workspace_used_when_no_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "plgt.utils.workspace_mode.config", _FakeConfig({"workspace": "dev"})
        )
        assert resolve_workspace_mode(from_workspace=None, from_registry=False) == "dev"

    def test_returns_none_when_no_flags_and_no_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("plgt.utils.workspace_mode.config", _FakeConfig({}))
        assert resolve_workspace_mode(from_workspace=None, from_registry=False) is None

    def test_mutual_exclusion_raises(self) -> None:
        with pytest.raises(ValidationError, match="mutually exclusive"):
            resolve_workspace_mode(from_workspace="dev", from_registry=True)
