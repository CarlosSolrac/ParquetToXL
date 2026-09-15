"""Fixtures and shared test support, depended on by every package's dev group.

A real workspace member rather than a directory under ``tests/`` so that the same import
resolves whether pytest runs from the repository root or from one package. Dev-only: it is
never a runtime dependency and is not measured by the coverage gate.
"""
