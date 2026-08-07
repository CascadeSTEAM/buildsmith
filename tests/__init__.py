"""Test package.

Present so `tests.fixtures` is importable the same way under `buildsmith test`,
`python3 -m unittest tests.test_audit` and `pytest`. Without it, discovery has
to treat `tests/` as its own top level, and a bare `import fixtures` then works
only under discovery — which is exactly the kind of difference that makes a test
suite pass in CI and fail for the person trying to run one test.
"""
