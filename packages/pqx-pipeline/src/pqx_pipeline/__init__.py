"""Selection, Parquet ingest and the write that produces a manifest.

The only place that chains stages and touches everything. Every other package here is a layer with
one job; this one is where they are put in an order, which is why it is also where the ordering
rules that matter -- verify before publish, sidecars before workbooks, receipt last -- are enforced
rather than merely described.
"""
