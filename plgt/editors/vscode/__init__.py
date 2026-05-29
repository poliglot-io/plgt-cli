"""VS Code extension bundle.

The ``poliglot-latest.vsix`` artifact is dropped into this package at build time
by CI and shipped as package-data (see ``pyproject.toml``). The runtime
lookup in ``plgt.cmd.init`` resolves it via ``importlib.resources``.
"""
