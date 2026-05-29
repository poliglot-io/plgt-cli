"""Editor integrations bundled with the Poliglot CLI.

Subpackages ship editor extensions (e.g. the VS Code ``.vsix``) as
package-data so ``plgt init`` can install them without a network round-trip.
The actual binary artifacts are produced at build time and dropped into
each editor's subpackage by CI; this directory exists so ``importlib.resources``
can resolve them at runtime.
"""
