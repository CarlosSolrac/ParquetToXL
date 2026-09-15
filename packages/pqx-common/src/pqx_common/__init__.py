"""Path construction and logging configuration, shared by every package here.

``zpath`` is the one seam through which storage credentials reach a path, and the only
place that knows a path may be remote. ``configure_logging`` is the one call that sets up
structlog over the standard library.
"""

from pqx_common.paths import ZPath, zpath

__all__ = ["ZPath", "zpath"]
