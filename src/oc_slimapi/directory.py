"""Directory normalisation (core helper).

Lives at package root so both core modules (e.g. ``children_cache``) and
route handlers can import it without a layering inversion (core importing
from routes).
"""

from __future__ import annotations

from .errors import CodedHTTPException


def normalize_directory(directory: str) -> str:
    """Strip trailing slash (keep root '/'). Pure; no allowlist check.

    slimapi no longer gates directories — any directory is forwarded to
    upstream opencode (which decides whether it can serve it). Normalisation
    is kept so forwarded ``X-Opencode-Directory`` headers and ``?directory=``
    query params stay consistent across endpoints and across callers.
    """
    return directory.rstrip("/") or "/"


def validate_directory(directory: str) -> str:
    """Normalize and validate a user-supplied directory string.

    Returns the normalized directory if valid.
    Raises ``CodedHTTPException(400, code="invalid_directory")`` on:
    - Path segment ``..`` or ``.`` (parent/current directory)
    - Null byte (``\\0``)
    - ASCII control characters (ord < 0x20 or == 0x7f)
    - Length > 4096
    """
    norm = normalize_directory(directory)

    # Reject path traversal
    if ".." in norm.split("/") or "." in norm.split("/"):
        raise CodedHTTPException(400, code="invalid_directory")

    # Reject null bytes
    if "\0" in norm:
        raise CodedHTTPException(400, code="invalid_directory")

    # Reject control characters (including DEL)
    for ch in norm:
        if ord(ch) < 0x20 or ord(ch) == 0x7f:
            raise CodedHTTPException(400, code="invalid_directory")

    # Reject too long
    if len(norm) > 4096:
        raise CodedHTTPException(400, code="invalid_directory")

    return norm
