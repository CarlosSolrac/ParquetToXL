"""Construction of :class:`upath.UPath` objects for local and cloud storage.

Every path in this library is built through :func:`zpath` rather than
``pathlib.Path`` or ``upath.UPath`` directly, so that credential handling has a
single seam to change.

Credentials
-----------
Explicitly supplied storage options are passed through untouched. Everything
else is left to ``adlfs``, which reads ``AZURE_STORAGE_ACCOUNT_NAME``,
``AZURE_STORAGE_ACCOUNT_KEY``, ``AZURE_STORAGE_CONNECTION_STRING``,
``AZURE_STORAGE_SAS_TOKEN`` and the service-principal variables from the
environment, and falls back to ``DefaultAzureCredential`` when none of them are
set. That fallback is what lets the same code authenticate through ``az login``
on a workstation and through a managed identity on a cluster, so duplicating it
here would only create a second, diverging code path.

PySpark
-------
Two rules apply when this library runs on Spark.

1. Spark does not use ``fsspec``. It reads ``abfss://`` through the Hadoop ABFS
   connector configured by ``spark.hadoop.fs.azure.account.*``, so the storage
   options set here govern driver-side Python I/O only -- polars, calamine,
   ``exists()``, ``iterdir()``. ``spark.read.parquet(str(path))`` uses the
   string and nothing else.

2. Never send a path object from the driver to an executor.
   ``UPath.__reduce__`` reconstructs a path from its storage options, so
   pickling a path pickles its credentials: an account key would travel inside
   the serialized task, and a live credential object would fail to pickle at
   all. Send ``str(path)`` instead and call :func:`zpath` again on the executor,
   which lets each executor authenticate from its own environment.
"""

from __future__ import annotations

import pickle
from collections.abc import Mapping
from typing import Any, Final

from upath import UPath
from upath.types import JoinablePathLike

__all__ = ["ZPath", "zpath"]


def _reject_unpicklable_storage_options(storage_options: Mapping[str, object]) -> None:
    """Reject storage options that cannot survive being sent to a Spark executor.

    ``UPath.__reduce__`` rebuilds a path from its storage options, so an option
    holding an event loop, a lock or a socket -- an ``azure.identity``
    credential, a ``boto3.Session``, a ``requests.Session`` -- raises deep
    inside ``pickle`` on the first task that captures the path, far from the
    call that caused it. Fail here instead.

    The check is by value rather than by option name, because the names differ
    per backend (``credential``, ``session``, ``token``, ``request_session``)
    and a named-key check needs a new special case for each one.

    Args:
        storage_options: The storage options of a constructed path, which
            include any inherited from a ``UPath`` passed as an argument.

    Raises:
        TypeError: If any option cannot be pickled.
    """
    offenders: list[str] = []
    key: str
    value: object
    for key, value in storage_options.items():
        try:
            pickle.dumps(value)
        # Any failure to pickle disqualifies the value, whatever it raises.
        except Exception:
            offenders.append(f"{key}={type(value).__name__}")
    if offenders:
        raise TypeError(
            f"storage options cannot be pickled: {', '.join(sorted(offenders))}. "
            "A path carrying them fails as soon as a Spark task captures it, far from this call. "
            "Pass account_name and let adlfs resolve DefaultAzureCredential on each machine, or pass a string credential such as account_key, sas_token or connection_string.",
        )


def zpath(
    *args: JoinablePathLike,
    protocol: str | None = None,
    storage_options: Mapping[str, object] | None = None,
    **kwargs: object,
) -> UPath:
    """Build a path for a local or cloud location.

    Returns the protocol-specific ``UPath`` implementation -- ``LocalPath`` for
    a filesystem path, ``AzurePath`` for ``az://`` and ``abfs://``, and so on --
    so the object behaves exactly as ``universal_pathlib`` intends. Local paths
    are ``pathlib.Path`` subclasses and therefore satisfy ``os.fspath()``, which
    is what lets them be handed to polars and calamine unchanged.

    Storage options may be given either as the ``storage_options`` mapping or as
    keyword arguments, which is how ``upath`` itself accepts them. Keyword
    arguments win where the two disagree.

    Args:
        *args: Path or URI segments. The first determines the protocol unless
            ``protocol`` is given.
        protocol: Explicit fsspec protocol, overriding any URI scheme in ``args``
            and any ``protocol`` key in the storage options.
        storage_options: fsspec storage options, including credentials.
        **kwargs: Further storage options, taking precedence over ``storage_options``.

    Returns:
        The path, as the ``UPath`` subclass registered for its protocol.

    Raises:
        TypeError: If any resulting storage option cannot be pickled.

    Examples:
        >>> zpath("az://container/data.parquet", account_name="myaccount").protocol
        'az'

    """
    options: dict[str, Any] = {**(storage_options or {}), **kwargs}
    if protocol is not None:
        # Forwarded only when set. UPath copies a lone UPath argument verbatim
        # but only when no keyword follows it; passing protocol=None instead
        # rebuilds the path from its string form, which turns a relative path
        # absolute against the wrong container.
        options["protocol"] = protocol
    path: UPath = UPath(*args, **options)
    _reject_unpicklable_storage_options(path.storage_options)
    return path


ZPath: Final = zpath
"""CapWords alias, so a call site can read as a constructor: ``ZPath("az://container/data.parquet")``.

Annotate paths as ``upath.UPath``; the factory returns whichever registered
implementation matches the protocol, so there is no ``ZPath`` type to name.
"""
