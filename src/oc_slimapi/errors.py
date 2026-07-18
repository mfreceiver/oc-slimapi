"""Structured thin-route errors.

Thin routes raise :class:`CodedHTTPException`; the registered handler renders
the body as ``{"code": string, **fields}`` (contract §11) instead of FastAPI's
default ``{"detail": ...}``. ``CodedHTTPException`` subclasses
``fastapi.HTTPException`` so existing control flow (helpers that ``raise``) is
unchanged — only the rendered body shape moves.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.responses import Response

from .gzip_util import json_response


class CodedHTTPException(HTTPException):
    """HTTPException whose body is ``{"code": ..., **fields}``.

    The ``detail`` arg of the parent is set to ``code`` purely so logs / any
    fallback HTTPException handler still show a meaningful identifier.
    """

    def __init__(self, status_code: int, *, code: str, **fields: Any) -> None:
        self.code = code
        self.fields = fields
        super().__init__(status_code=status_code, detail=code)


async def coded_exception_handler(request: Request, exc: CodedHTTPException) -> Response:
    return json_response(
        {"code": exc.code, **exc.fields},
        status_code=exc.status_code,
        accept_encoding=request.headers.get("accept-encoding"),
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register the CodedHTTPException handler.

    Called from ``oc_slimapi.app`` (production) AND from every test ``_build_app``
    helper — tests bypass the module-level app construction, so they must wire
    the handler themselves or CodedHTTPException would render as ``{"detail":...}``.
    """
    app.add_exception_handler(CodedHTTPException, coded_exception_handler)
