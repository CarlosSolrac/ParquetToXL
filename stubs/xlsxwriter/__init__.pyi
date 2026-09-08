"""Minimal local stub for XlsxWriter, which ships neither ``py.typed`` nor typeshed stubs.

Audited in Phase 0: of the seven runtime dependencies, this is the only one without inline
types, and there is no ``types-xlsxwriter`` or ``xlsxwriter-stubs`` on PyPI (checked, 404).

Deliberately covers only ``Workbook``, and only as a type. This project never constructs a
workbook: ``PolarsExcelWriter`` calls ``df.write_excel(workbook=str(path), ...)`` and polars
-- which is ``py.typed`` -- owns the xlsxwriter interaction from there. Declaring
``__init__`` or the writer methods here would be inventing a signature for a call site that
does not exist, and a speculative stub type-checks silently when it is wrong.

If a later ticket genuinely needs a member, add that member then, against the real
signature. An access to anything not declared here is a hard error, which is the intended
behaviour: it means the assumption above stopped holding.
"""

class Workbook: ...
