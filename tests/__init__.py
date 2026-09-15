"""Marks the cross-package integration tree as a package.

The original reason was the fixture generator: without a package marker mypy reached it by
two module names at once -- ``generate`` and ``fixtures.generate`` -- and refused to check
anything. That generator now lives in ``pqx_testing`` and is imported by its distribution
name, so the ambiguity is gone.

The marker stays because the same collision would return the moment two packages each grow
a ``tests/conftest.py`` or a same-named test module. Per-package test directories are
deliberately *not* packages: pytest's rootdir-relative import mode gives each one a unique
name already, and adding ``__init__.py`` under every ``packages/*/tests/`` would put twenty
test modules back into one namespace.
"""
