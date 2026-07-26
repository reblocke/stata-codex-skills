"""Pinned Python and Unicode runtime checks for executable skill-pack gates."""

from __future__ import annotations

import sys
import unicodedata


REQUIRED_PYTHON = (3, 11)
REQUIRED_UNICODE_VERSION = "14.0.0"


def runtime_compatibility_error(
    version_info: tuple[int, ...] | None = None,
    unicode_version: str | None = None,
) -> str | None:
    """Return a deterministic-runtime error, or None for the pinned runtime."""

    observed_version = tuple(
        (sys.version_info if version_info is None else version_info)[:2]
    )
    observed_unicode = (
        unicodedata.unidata_version
        if unicode_version is None
        else unicode_version
    )
    if (
        observed_version == REQUIRED_PYTHON
        and observed_unicode == REQUIRED_UNICODE_VERSION
    ):
        return None
    return (
        "Python 3.11.x with Unicode "
        f"{REQUIRED_UNICODE_VERSION} is required; found Python "
        f"{observed_version[0]}.{observed_version[1]} with Unicode "
        f"{observed_unicode}"
    )


def require_supported_runtime() -> None:
    """Fail a CLI before work when its Unicode behavior is not reproducible."""

    error = runtime_compatibility_error()
    if error is not None:
        raise SystemExit(f"ERROR: {error}")
