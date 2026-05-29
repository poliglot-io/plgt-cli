# Broken-matrix fixture corpus

One subdirectory per `PLGT_E####` diagnostic code. Each fixture is a minimal
matrix project whose contents are crafted to make exactly that diagnostic fire.
`test_broken_fixtures.py` walks this tree, scaffolds a synthetic engine cache
around each fixture in a `tmp_path`, runs `validate_project`, and asserts the
expected code appears among the diagnostics.

Add a new fixture by:

1. `mkdir broken/E0XXX/spec`
2. Drop a `poliglot.yml` and TTL files designed to trigger the code.
3. Add `"E0XXX"` to the parameter list in `test_broken_fixtures.py`.
