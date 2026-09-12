"""Canonical value encoding and order-independent dataframe hashers.

``HashedDataframe`` is defined here rather than in ``base.py``, which is where the spec's
layout put it. It cannot live there: ``base.py`` would need the concrete
``BinaryAggregateHashedDataframe`` to build the alias, while ``binary_aggregate.py`` needs
``HashedDataframeBase`` and ``DataFrameHasherBaseClass`` from ``base.py`` — a genuine
import cycle, which fails at runtime with "cannot import name ... from partially
initialized module".

This package module is the natural home instead: it is the one place that knows every
member of the union. Adding a second hasher means adding its model to the union here and
nowhere else, so ``base.py`` never learns about any concrete hasher.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from parquet_to_xl.hashing.binary_aggregate import BinaryAggregateHashedDataframe

type HashedDataframe = Annotated[BinaryAggregateHashedDataframe, Field(discriminator="identifier")]
"""The hash record type consumers annotate against.

A one-member discriminated union today. Pydantic accepts that and it widens to
``A | B`` without any consumer changing, which is the point: ``DataframeColumnMetadata``
declares ``hashes: list[HashedDataframe]`` and keeps working when a second hasher lands.
"""

__all__ = ["BinaryAggregateHashedDataframe", "HashedDataframe"]
