"""Directory normalisation (core helper).

Lives at package root so both core modules (e.g. ``children_cache``) and
route handlers can import it without a layering inversion (core importing
from routes).
"""

from __future__ import annotations


def normalize_directory(directory: str) -> str:
    """Strip trailing slash (keep root '/'). Pure; no allowlist check.

    slimapi no longer gates directories — any directory is forwarded to
    upstream opencode (which decides whether it can serve it). Normalisation
    is kept so forwarded ``X-Opencode-Directory`` headers and ``?directory=``
    query params stay consistent across endpoints and across callers.
    """
    return directory.rstrip("/") or "/"
