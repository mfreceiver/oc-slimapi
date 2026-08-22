"""P5 raw file-content route.

The upstream ``/file/content`` endpoint returns a ``LegacyContent`` envelope.
This route admits the request before fetching that envelope, then performs the
decode, representation packing, and validator work under the strict transform
permit.  A cancelled request therefore cannot abandon an executor item while
also returning its permit to the admission pool.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

import httpx
import orjson
from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from .. import etag as etag_mod
from ..errors import CodedHTTPException
from ..gzip_util import error_response, compress_if_beneficial
from ..selector import _strip_query_keys, _strip_v_segments
from ..traffic import stash_up_in
from ..transform import TransformBusy, read_with_cap
from ._catalog_common import busy_response, stream_upstream
from .read_groups import _authorized_file_directory, _resolve


router = APIRouter(prefix="/slimapi", tags=["file-raw"])

_MIME_RE = re.compile(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+")
_UPSTREAM_HEADERS = (
    "content-type",
    "location",
    "retry-after",
    "x-request-id",
    "last-request-id",
)


class _RawDecodeFailed(ValueError):
    """The successful upstream body was not a valid LegacyContent envelope."""


@dataclass(frozen=True, slots=True)
class _RawRepresentation:
    body: bytes
    headers: dict[str, str]
    not_modified: bool


def _decode_legacy_content(raw: bytes) -> tuple[bytes, str, bool]:
    """Decode a validated ``LegacyContent`` envelope to identity bytes.

    The boolean in the result marks text content.  Binary content is always
    served as identity bytes, even when the client advertises gzip.
    """
    try:
        value = orjson.loads(raw)
        if not isinstance(value, dict):
            raise _RawDecodeFailed("envelope is not an object")
        kind = value.get("type")
        content = value.get("content")
        if not isinstance(content, str):
            raise _RawDecodeFailed("content is not text")
        if kind == "text":
            return content.encode("utf-8"), "text/plain; charset=utf-8", True
        if kind != "binary":
            raise _RawDecodeFailed("unknown content type")
        try:
            identity = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise _RawDecodeFailed("invalid binary content") from exc
        mime = value.get("mimeType")
        if not isinstance(mime, str) or _MIME_RE.fullmatch(mime) is None:
            mime = "application/octet-stream"
        return identity, mime, False
    except _RawDecodeFailed:
        raise
    except (UnicodeError, TypeError, ValueError, orjson.JSONDecodeError) as exc:
        raise _RawDecodeFailed("malformed content envelope") from exc


def _transform_legacy_content(
    raw: bytes,
    *,
    accept_encoding: str | None,
    if_none_match: str | None,
    rep_version: bytes | None,
) -> _RawRepresentation:
    """Parse, pack, and validate one LegacyContent body on a worker thread."""
    identity, content_type, is_text = _decode_legacy_content(raw)
    if is_text:
        body, coding_headers = compress_if_beneficial(
            identity, accept_encoding,
        )
    else:
        body = identity
        coding_headers = {"Vary": "Accept-Encoding"}

    headers = {
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        **coding_headers,
    }
    if rep_version is not None:
        coding = "gzip" if headers.get("Content-Encoding") == "gzip" else "identity"
        headers["ETag"] = etag_mod.compute_etag(
            identity, coding, rep_version,
        )
        if etag_mod.if_none_match_matches(if_none_match, headers["ETag"]):
            return _RawRepresentation(b"", headers, True)
    return _RawRepresentation(body, headers, False)


def _raw_upstream_url(request: Request) -> str:
    """Strip sidecar-owned query keys while preserving all other raw bytes."""
    query = request.scope.get("query_string", b"") or b""
    query = _strip_v_segments(query)
    query = _strip_query_keys(query, frozenset({"directory"}))
    if query:
        return f"/file/content?{query.decode('latin-1')}"
    return "/file/content"


def _upstream_error_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in _UPSTREAM_HEADERS
        if name in response.headers
    }


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _raw_error(
    code: str,
    status_code: int,
    *,
    accept_encoding: str | None = None,
    **fields,
) -> Response:
    return _no_store(error_response(
        code, status_code,
        accept_encoding=accept_encoding,
        **fields,
    ))


@router.get("/file/raw")
async def file_raw(
    request: Request,
    path: str | None = Query(None),
    directory: str | None = None,
):
    """Return raw bytes for an upstream ``LegacyContent`` response."""
    # Gate-MAJOR-2.1 (§19): ``path`` is a required query parameter — its
    # absence is a 400 ``invalid_params`` (NOT FastAPI's default 422). The
    # manual check runs before the transform permit is acquired, so a
    # malformed request never consumes admission. An EMPTY ``?path=`` value
    # is present-but-empty and keeps travelling verbatim to upstream.
    if path is None:
        raise CodedHTTPException(
            400,
            code="invalid_params",
            headers={"Cache-Control": "no-store"},
        )
    config = request.app.state.config
    effective_cap = min(
        config.max_response_bytes,
        config.file_raw_max_envelope_bytes,
    )
    accept_encoding = request.headers.get("accept-encoding")
    if_none_match = request.headers.get("if-none-match")
    rep_version = etag_mod.response_rep_version(config, wire_view=4)
    pool = request.app.state.transforms

    try:
        await pool.acquire()
    except TransformBusy:
        return _no_store(busy_response(accept_encoding))

    response: httpx.Response | None = None
    strict_owns_permit = False
    try:
        try:
            resolved = _resolve(request, directory)
            # Gate-R2-MAJOR: the directory allowlist gate raises
            # ``CodedHTTPException(403, "directory_not_allowed")`` without
            # headers, and the GLOBAL CodedHTTPException renderer does NOT
            # stamp ``Cache-Control: no-store`` (verified — it only merges
            # ``exc.headers``).  Re-render locally through ``_raw_error`` —
            # same body/status/fields as the global rendering, plus the
            # §19-constant no-store.  Same pattern as the Gate-MAJOR-2.3
            # stream_upstream excepts below; global renderer/read_groups
            # untouched.  (Also catches ``_resolve``'s defensive
            # ``invalid_directory`` 400 — same local-no-store rationale.)
            forward_directory = _authorized_file_directory(request, resolved)
        except CodedHTTPException as exc:
            return _raw_error(
                exc.code, exc.status_code,
                accept_encoding=accept_encoding,
                **exc.fields,
            )
        try:
            response = await stream_upstream(
                request,
                _raw_upstream_url(request),
                forward_directory,
            )
        except httpx.RequestError:
            # Gate-MAJOR-2.3: the GLOBAL CodedHTTPException renderer does
            # NOT stamp ``Cache-Control: no-store`` (verified — it only
            # merges ``exc.headers``), and the global renderer is out of
            # bounds for this fix.  Build the 503 locally instead: same
            # ``{"code": "upstream_unavailable"}`` body (plus gzip
            # negotiation) as ``raise_upstream_unavailable`` would render,
            # but through ``_raw_error`` so ``no-store`` is guaranteed.
            # The ``finally`` below still closes the (possibly partial)
            # upstream response and releases the permit.
            return _raw_error(
                "upstream_unavailable", 503,
                accept_encoding=accept_encoding,
            )
        except CodedHTTPException as exc:
            # Gate-MAJOR-2.3, initial-send path: the SHARED
            # ``stream_upstream`` helper (``_catalog_common`` — outside this
            # fix's write domain) already converts every httpx.RequestError
            # into ``CodedHTTPException(503, upstream_unavailable)`` before
            # it can reach the branch above.  Re-render that coded error
            # through ``_raw_error`` for the route-local no-store stamp;
            # body/status/fields stay identical to the global rendering.
            return _raw_error(
                exc.code, exc.status_code,
                accept_encoding=accept_encoding,
                **exc.fields,
            )

        if response.status_code >= 500:
            return _raw_error(
                "upstream_unavailable", 503,
                accept_encoding=accept_encoding,
            )
        if response.status_code >= 400:
            try:
                body, _ = await read_with_cap(
                    response,
                    effective_cap,
                    on_read=lambda n: stash_up_in(request, n),
                )
            except httpx.RequestError:
                # Gate-MAJOR-2.3 (4xx branch, mid-read network failure):
                # local no-store 503, same rationale as above.
                return _raw_error(
                    "upstream_unavailable", 503,
                    accept_encoding=accept_encoding,
                )
            if body is None:
                return _raw_error(
                    "response_too_large", 413,
                    accept_encoding=accept_encoding,
                    limit=effective_cap,
                )
            # Gate-MAJOR-2.2: upstream 4xx passes through VERBATIM (status +
            # allowlisted headers + body bytes) but is still an error frame
            # for this route — stamp ``no-store`` on it like every other
            # error response here.
            return _no_store(Response(
                body,
                status_code=response.status_code,
                headers=_upstream_error_headers(response),
            ))

        try:
            body, _ = await read_with_cap(
                response,
                effective_cap,
                on_read=lambda n: stash_up_in(request, n),
            )
        except httpx.RequestError:
            # Gate-MAJOR-2.3 (2xx branch, mid-read network failure): local
            # no-store 503, same rationale as the initial-send branch.
            return _raw_error(
                "upstream_unavailable", 503,
                accept_encoding=accept_encoding,
            )
        if body is None:
            return _raw_error(
                "response_too_large", 413,
                accept_encoding=accept_encoding,
                limit=effective_cap,
            )

        # Once submission begins, offload_strict owns the permit.  The route
        # must not release it in its cancellation/error cleanup path.
        strict_owns_permit = True
        try:
            representation = await pool.offload_strict(
                _transform_legacy_content,
                body,
                accept_encoding=accept_encoding,
                if_none_match=if_none_match,
                rep_version=rep_version,
            )
        except _RawDecodeFailed as exc:
            raise CodedHTTPException(
                502,
                code="raw_decode_failed",
                headers={"Cache-Control": "no-store"},
            ) from exc

        if representation.not_modified:
            return etag_mod.not_modified_response(
                representation.headers["ETag"],
                representation.headers["Vary"],
            )
        return Response(
            representation.body,
            status_code=response.status_code,
            headers=representation.headers,
        )
    finally:
        if response is not None:
            await response.aclose()
        if not strict_owns_permit:
            pool.release()
