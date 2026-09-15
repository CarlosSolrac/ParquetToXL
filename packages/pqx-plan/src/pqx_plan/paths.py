"""The destination-relative path type every location in a manifest carries."""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import AfterValidator

PARENT_SEGMENT: Final = ".."
"""The segment that would take a manifest entry outside the directory it is relative to."""

SEPARATORS: Final = ("/", "\\")
"""Both separators are treated as one. Names are generated on the Linux host that writes the
manifest, but the destination may be SMB, so a backslash reaches this as an ordinary character
and a ``..`` behind one would otherwise pass unseen."""

DRIVE_COLON_POSITION: Final = 1
"""Where a Windows drive letter's colon sits, as in ``C:\\out``."""


def require_destination_relative(value: str) -> str:
    """Refuse a path that is not relative to the destination, or that escapes it.

    ``export-pipeline-spec.md`` states this as a property the manifest has rather than one it
    enforces, and two separate things spend it. Verification joins each path to the scratch
    root, and reconciliation joins the same path to the destination, which is what lets one
    manifest serve both and survive the destination being moved. An absolute path silently
    wins that join in ``PurePath`` and in ``posixpath`` alike -- ``Path("/a") / "/b"`` is
    ``/b`` -- so it would not fail, it would read and delete somewhere else.

    A ``..`` segment is refused for the harder half of the same reason. Reconciliation
    computes its delete set from a directory listing minus the manifest's own entries, so a
    manifest entry resolving above the destination is an instruction to delete outside the
    directory the ownership check cleared.

    A Windows drive letter and a URI scheme are both refused as absolute: the destination may
    be SMB, Azure or POSIX, and the run that writes the manifest is on Linux, where neither form
    looks absolute to the standard library at all. The drive rule over-refuses by exactly one
    case -- a single-character relative name followed by a colon, which POSIX permits -- and
    that is deliberate rather than overlooked: a colon is illegal in a name on both of the other
    two destinations, so such a path could never be published anyway.

    Args:
        value: The recorded path.

    Returns:
        ``value`` unchanged. Nothing is normalised -- the string is compared against a remote
        listing, and rewriting it here would make the manifest disagree with what was written.

    Raises:
        ValueError: The path is empty, absolute, drive-qualified, carries a URI scheme, or
            holds a ``..`` segment.
    """
    if not value:
        message: str = "a manifest path may not be empty"
        raise ValueError(message)
    if value.startswith(SEPARATORS):
        message = f"{value!r} is absolute; manifest paths are relative to the destination"
        raise ValueError(message)
    if len(value) > DRIVE_COLON_POSITION and value[DRIVE_COLON_POSITION] == ":":
        message = f"{value!r} is drive-qualified; manifest paths are relative to the destination"
        raise ValueError(message)
    if "://" in value:
        message = f"{value!r} carries a URI scheme; manifest paths are relative to the destination"
        raise ValueError(message)
    if PARENT_SEGMENT in value.replace(SEPARATORS[1], SEPARATORS[0]).split(SEPARATORS[0]):
        message = f"{value!r} holds a {PARENT_SEGMENT!r} segment, which would resolve outside the destination"
        raise ValueError(message)
    return value


type DestinationRelativePath = Annotated[str, AfterValidator(require_destination_relative)]
"""A path recorded in a manifest, relative to wherever the manifest is joined against."""
