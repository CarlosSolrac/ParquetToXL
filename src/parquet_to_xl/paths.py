"""The one path constructor this library uses, and the seam for future credential wiring."""

from __future__ import annotations

from upath import UPath

ZPath: type[UPath] = UPath
"""The constructor every call site in this library builds paths with, never ``Path`` directly.

Annotations use ``UPath`` rather than ``ZPath``. That split is forced rather than chosen: a
name that can be *called* has to be a plain binding, while a name usable in an annotation has
to be a ``TypeAlias`` or a PEP 695 ``type`` statement -- and a PEP 695 alias is not callable,
so no single name can be both. The seam is the construction point, which is the part that has
to be single; the annotation is just ``UPath``, which is what a constructed path always is.

The spec originally asked for a thin ``class ZPath(UPath)`` overriding ``__init__`` to pop and
store a ``storage_options`` mapping, as the single future seam for Azure and AWS credentials.
That cannot be built on ``universal-pathlib`` 0.3.10, for architectural reasons rather than
incidental ones, all measured:

- UPath selects **one concrete class per protocol** from a registry -- ``""`` resolves to
  ``WindowsUPath``, ``"s3"`` to ``S3Path``. Constructing a bare ``class ZPath(UPath)`` raises
  ``_IncompatibleProtocolError`` for every protocol, the empty local one included.
- Subclassing the concrete local class instead fails identically.
- Registering a subclass works only for a protocol of its own, so it cannot be the single
  type spanning local and cloud paths that the spec intends.

The seam survives anyway, because what it existed for is already here: UPath 0.3.10 accepts
``**storage_options`` natively and exposes them, so ``ZPath("s3://b/k", anon=True)`` carries
credentials with no subclass at all. This binding is the seam -- one name, one place. Wiring
real credentials later changes this line, not every call site.

It is a binding rather than a factory function for a mechanical reason worth recording so
that nobody "fixes" it: a function named ``ZPath`` trips Ruff's ``N802``, and a factory class
whose ``__new__`` returns a ``UPath`` is rejected by mypy, which requires ``__new__`` to
return a subtype of its own class. Both of those routes need a suppression. This one needs
none.
"""

__all__ = ["ZPath"]
