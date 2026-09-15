"""What a run did, as a model and as three renderings of it.

Presentation, kept out of the pipeline's dependency set. Pure: a result in, text out. Nothing here
reads a clock, opens a file or knows what a destination looks like -- ``pqx-pipeline`` constructs
the report and decides where it goes, which is also what avoids the cycle a pipeline-owned
``RunReport`` would create.
"""
