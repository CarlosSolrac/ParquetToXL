"""Process-wide logging configuration: structlog rendering over the standard library."""

from __future__ import annotations


def configure_logging(json_output: bool) -> None:
    """Configure structlog and the standard library's logging for this process.

    One call makes logging work process-wide. The configuration is exactly:

    - ``structlog.configure`` is called with ``structlog.stdlib.LoggerFactory()`` as the
      logger factory, ``structlog.stdlib.BoundLogger`` as the wrapper class, and
      ``cache_logger_on_first_use`` false.
    - The processor chain is ``structlog.processors.add_log_level``, then
      ``structlog.processors.TimeStamper(fmt="iso")``, then the renderer.
    - The renderer is ``structlog.processors.JSONRenderer()`` when ``json_output`` is true
      and ``structlog.dev.ConsoleRenderer()`` when it is false.
    - The root standard-library logger has its handlers replaced by exactly one
      ``logging.StreamHandler`` writing to ``sys.stdout``, and its level set to
      ``logging.INFO``.

    Three consequences follow, and each is pinned by a test. Logging through structlog and
    logging through ``logging.getLogger()`` reach the same destination, so a third-party
    library's output is not lost. Calling this function repeatedly replaces the previous
    configuration rather than adding to it, so handlers do not multiply and output is not
    duplicated. And because loggers are not cached on first use, a later call changes the
    behaviour of a logger that was obtained before it.

    Args:
        json_output: Render each event as one JSON object when true, which is what a log
            collector expects. Render human-readable console output when false.
    """
    raise NotImplementedError
