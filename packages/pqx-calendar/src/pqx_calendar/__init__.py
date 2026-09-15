"""Date-column interpretation, period keys, period labels and calendar grids.

The package is deliberately pure: no Polars, no file access, no frame types. Everything
here is a value in and a value out, which is what lets the largest test matrix in the
project -- ``YY`` century boundaries, leading zeros, cross-year ranges, month-precision
refusals, timezone year boundaries -- live beside the rules it checks rather than inside
the planner's tests.
"""
