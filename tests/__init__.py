"""Marks the test tree as a package.

Without these markers mypy reaches ``tests/fixtures/generate.py`` by two different module
names at once -- ``generate`` and ``fixtures.generate`` -- and refuses to check anything:
"Source file found twice under different module names". Package markers give every test
module one unambiguous name, and let the fixture generator be imported as
``tests.fixtures.generate`` from anywhere in the tree.
"""
