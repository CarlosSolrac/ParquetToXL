"""Checking a written workbook against the sidecar recorded before it existed.

The integration seam: the only place that pulls the conversion, the hasher, the reader and
the sidecar store together, and the only place that names concrete implementations rather
than taking them from a registry. It lives outside ``pqx_sidecar`` so that persisting a
record does not drag in an Excel reader.
"""
