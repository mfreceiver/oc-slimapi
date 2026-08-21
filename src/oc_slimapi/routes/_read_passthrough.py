"""Shared §10.a read-group controlled-proxy pipeline (v3 Batch C1).

The 7 read groups annexed by ``docs/specs/v3-contract.md`` §10.a (file /
vcs / find / providers / session-single / active / global-health) share one
chain, factored here:

* **verbatim raw-query forwarding (§5.2)**: the post-selector raw query
  bytes — ``v`` stripped on every ``/slimapi/**`` request, ``directory``
  additionally stripped on v3 consuming routes — are embedded in the
  upstream URL so httpx never re-encodes them (proxy.py:196-203
  semantics). Unknown params, repeats, percent-encodings and ``+`` survive
  byte-identically. Tolerant routes keep ``directory`` verbatim (no
  consumption, §5.5); consuming routes had it stripped by the selector
  (§5.2).
* streaming upstream GET (``stream_upstream`` — resolved directory
  forwarded as ``X-Opencode-Directory``);
* two-tier frozen error mapping: upstream 5xx / network → 503
  ``upstream_unavailable``; upstream 4xx → **status + body verbatim**
  passthrough (client validation errors arrive raw). Both error-body reads
  are cap-protected — over ``max_response_bytes`` the verbatim duty
  degrades to 503 (§10.a:141: resource protection wins);
* success status passes through verbatim (201/202/204/206/3xx); redirects
  are never followed (``follow_redirects=False``); bodiless successes
  naturally drop ETag/gzip;
* projection routes (session single) are gated + pooled + offloaded per
  §10.a:141 — only 2xx non-empty legal-JSON-object bodies project, under
  transform-pool admission (saturation → 503 ``transform_busy``), with the
  parse/project/serialize running on a worker; everything else passes
  through verbatim;
* §10.a frozen response-header passthrough set (2xx and 4xx alike):
  ``Content-Type`` / ``Location`` / ``Retry-After`` / ``X-Request-ID`` /
  ``Last-Request-ID``. Upstream ``Content-Encoding`` is never passed —
  httpx has already decoded the entity and the sidecar re-encodes under
  its own gzip gate + ETag domain (entity-byte semantics);
* cap-read (``read_with_cap`` → 413 ``response_too_large``);
* §10.a ETag enablement (contract line "§10.a 全集 GET 启用"): coding-derived
  validator on the identity body with the wire-view domain marker (§6.1),
  304 via ``If-None-Match`` weak compare, gzip re-encode negotiated;
* per contract §10.a admission rule, pure-raw controlled proxies do NOT
  occupy the transform pool (no projection → no ``transform_busy``); the
  tail's hashing/gzip runs inline for small bodies and leaves the event
  loop via ``asyncio.to_thread`` (the DEFAULT executor — never the
  transform pool) at/above ``_TAIL_OFFLOAD_MIN_BYTES`` (F-202).

Vary: directory-sensitive routes (consuming set) emit the merged double
value ``Accept-Encoding, X-Opencode-Directory``; tolerant routes emit
``Accept-Encoding`` only.  ``Cache-Control: no-store`` on success and 304;
upstream ETag headers are never passed through (sidecar-owned domain).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx
from fastapi import Request
from starlette.responses import Response

from .. import etag as etag_mod
from ..gzip_util import compress_if_beneficial, error_response
from ..selector import _strip_v_segments
from ..traffic import stash_up_in
from ..transform import TransformBusy, TransformPool, read_with_cap
from ..upstream_errors import raise_upstream_unavailable
from ._catalog_common import busy_response, stream_upstream

# §10.a frozen response-header passthrough set: the entity's Content-Type
# plus redirect / retry / tracing headers.  ``Content-Encoding`` is
# deliberately absent — httpx already decoded the entity and the sidecar
# re-encodes under its own gate, so the upstream's coding header must
# never leak downstream.
_PASSTHROUGH_UPSTREAM_HEADERS = (
    "content-type",
    "location",
    "retry-after",
    "x-request-id",
    "last-request-id",
)

# F-202: bodies at/above this size run the judge/gzip/validator tail off
# the event loop (``asyncio.to_thread``). gzip level-6 throughput is
# ~20-60 MB/s single-core → a 1 MiB body costs ~17-50 ms inline (worth
# offloading); below it the whole tail (sha256 ~1.5-2 GB/s + gzip) is
# single-digit milliseconds and an executor round-trip costs more than it
# saves. Both branches call the SAME pure function — byte-identical output.
_TAIL_OFFLOAD_MIN_BYTES = 1 << 20


def _tail_encode(
    body: bytes, *, accept_encoding: str | None,
    if_none_match: str | None, rep_version: bytes | None,
) -> tuple[str | None, bytes, dict[str, str], str | None]:
    """F-202 response tail: 304 judgment + gzip + validator (§10.a chain).

    Same contract as ``routes.messages._judge_pack_tail`` (Wave 2 design
    §2.2): returns ``(verdict, encoded, coding_headers, etag_value)`` where
    a non-None ``verdict`` (tag or ``"*"``) means 304 with ``etag_value``
    the validator to echo, and ``None`` means the 200 payload is ready.
    The ``rep_version is not None and body`` guard mirrors the historical
    inline tail exactly — bodiless successes (204/3xx etc.) never judge
    and never carry an ETag. Pure CPU; runs inline below
    ``_TAIL_OFFLOAD_MIN_BYTES`` and via ``asyncio.to_thread`` above.
    """
    verdict: str | None = None
    if rep_version is not None and body:
        verdict = etag_mod.judge_conditional(
            body, if_none_match, rep_version,
            accept_encoding=accept_encoding,
        )
        if verdict == "*":
            _, coding = compress_if_beneficial(body, accept_encoding)
            actual = "gzip" if "Content-Encoding" in coding else "identity"
            return (
                "*", b"", {},
                etag_mod.compute_etag(body, actual, rep_version),
            )
        if verdict is not None:
            return verdict, b"", {}, verdict
    encoded, coding = compress_if_beneficial(body, accept_encoding)
    etag_value: str | None = None
    if rep_version is not None and body:
        actual = "gzip" if "Content-Encoding" in coding else "identity"
        etag_value = etag_mod.compute_etag(body, actual, rep_version)
    return None, encoded, coding, etag_value


def _upstream_passthrough_headers(
    response: httpx.Response,
    *,
    default_content_type: str | None = "application/json",
) -> dict[str, str]:
    """The frozen header set as present on the upstream response.

    ``default_content_type`` (C2 gate follow-up): the §10.a read groups
    keep the historical ``application/json`` default when the upstream
    omits Content-Type; §10.b write routes pass ``None`` for strictly
    present-only semantics (the frozen set must not gain a value the
    upstream never sent). C1 behaviour is byte-identical with the default.
    """
    kept: dict[str, str] = {}
    for name in _PASSTHROUGH_UPSTREAM_HEADERS:
        value = response.headers.get(name)
        if value is not None:
            kept[name] = value
    if default_content_type is not None:
        kept.setdefault("content-type", default_content_type)
    return kept


def _raw_upstream_url(request: Request, upstream_path: str) -> str:
    """§5.2 verbatim-query upstream URL.

    The selector has already done the view fork (terminal: only v3
    semantics exist): consuming routes had ``directory`` stripped;
    tolerant routes keep it (no consumption, §5.5). Stripping ``v``
    again here is idempotent — it covers selector-less stacks so the
    sidecar-reserved parameter can never reach the upstream.
    """
    raw_qs = request.scope.get("query_string", b"") or b""
    raw_qs = _strip_v_segments(raw_qs)
    if raw_qs:
        return f"{upstream_path}?{raw_qs.decode('latin-1')}"
    return upstream_path


@asynccontextmanager
async def _maybe_pool(
    transforms: TransformPool | None,
) -> AsyncIterator[TransformPool | None]:
    """Admission wrapper: pooled for projection routes, transparent for raw.

    ``TransformBusy`` raised on acquisition propagates to the caller's
    handler (→ ``transform_busy``). Raw routes (``transforms=None``) never
    touch the pool — §10.a admission freeze for pure-raw controlled
    proxies (no projection → no ``transform_busy``).
    """
    if transforms is None:
        yield None
    else:
        async with transforms as pool:
            yield pool


async def _read_error_body(request: Request, response: httpx.Response) -> bytes:
    """Cap-protected error-body read (§10.a:141 frozen).

    The verbatim 4xx duty presupposes a safely readable body: the read is
    cap-bounded by ``max_response_bytes``; over the limit the verbatim
    duty degrades to 503 ``upstream_unavailable`` (resource protection
    wins — the sidecar never buffers an unbounded error entity).
    """
    config = request.app.state.config
    try:
        err, _ = await read_with_cap(
            response, config.max_response_bytes,
            on_read=lambda n: stash_up_in(request, n))
    except httpx.RequestError as exc:
        raise_upstream_unavailable(exc)
    if err is None:
        raise_upstream_unavailable(RuntimeError("oversized upstream error body"))
    return err


async def read_passthrough_get(
    request: Request,
    *,
    upstream_path: str,
    directory: str | None = None,
    project: Callable[[bytes], bytes] | None = None,
) -> Response:
    """Controlled-proxy GET for a §10.a read group.

    ``project`` (optional) maps the raw upstream body bytes to the canonical
    identity bytes (used by the session-single skeleton projection). It runs
    on the shared chain under three §10.a:141 frozen rules:

    * **pooled**: projection is transform work — the slot is acquired
      BEFORE the upstream GET and held across fetch→read→project
      (``sessions.py`` admission pattern); saturation → 503
      ``transform_busy`` (``Retry-After``), never an event-loop fallback;
    * **gated**: projected only for ``2xx`` with a non-empty body —
      204/3xx and any other status pass through verbatim, unprojected;
    * **offloaded**: the parse+project+serialize runs on a worker thread
      (``pool.offload``); ``ValueError`` (bad JSON / non-object) → 503
      ``upstream_unavailable`` (2xx-but-malformed is an upstream breach).

    Raw routes (``project=None``) skip the pool entirely. Default:
    identity passthrough.
    """
    config = request.app.state.config
    accept_encoding = request.headers.get("accept-encoding")
    if_none_match = request.headers.get("if-none-match")
    # (V2b note: the historical wire_view=3 literal is KEPT deliberately —
    # the passthrough ETag domain (REP_VERSION marker "wire=v3") is frozen
    # into tests/golden/offload-baseline-v1.json (vcs_* cases); flipping the
    # marker would change observable ETag bytes. It is a domain label, not
    # a v3 flow: this stack never consults wire_view_from_scope.)
    rep_version = etag_mod.response_rep_version(config, wire_view=3)
    # §6.2 terminal: single-value Vary on every route.
    vary = etag_mod.merged_vary("Accept-Encoding")
    upstream_url = _raw_upstream_url(request, upstream_path)
    # B4: projection routes occupy the transform pool; raw routes don't.
    transforms = (request.app.state.transforms
                  if project is not None else None)

    try:
        async with _maybe_pool(transforms) as pool:
            try:
                response = await stream_upstream(
                    request, upstream_url, directory)
            except httpx.RequestError as exc:
                raise_upstream_unavailable(exc)
            try:
                status = response.status_code
                if status >= 500:
                    await _read_error_body(request, response)
                    raise_upstream_unavailable(
                        RuntimeError(f"upstream {status}"))
                if status >= 400:
                    # Frozen two-tier rule: upstream 4xx passes through
                    # verbatim (status + body + the frozen header set) —
                    # the body read itself is cap-protected (§10.a:141).
                    # No ETag/Vary/Cache-Control additions — the client
                    # sees the raw error.
                    err = await _read_error_body(request, response)
                    return Response(
                        err,
                        status_code=status,
                        headers=_upstream_passthrough_headers(response),
                    )
                try:
                    body, _ = await read_with_cap(
                        response, config.max_response_bytes,
                        on_read=lambda n: stash_up_in(request, n))
                except httpx.RequestError as exc:
                    raise_upstream_unavailable(exc)
                if body is None:
                    return error_response(
                        "response_too_large", 413,
                        accept_encoding=accept_encoding,
                        limit=config.max_response_bytes,
                    )
                # B5: project only 2xx + non-empty body (204/3xx etc.
                # pass through verbatim, unprojected); off the event loop.
                if (project is not None and pool is not None
                        and 200 <= status < 300 and body):
                    try:
                        body = await pool.offload(project, body)
                    except ValueError:
                        raise_upstream_unavailable(
                            RuntimeError("malformed upstream body"))
            finally:
                await response.aclose()
    except TransformBusy:
        return busy_response(accept_encoding)

    if len(body) >= _TAIL_OFFLOAD_MIN_BYTES:
        # F-202: large bodies leave the event loop via the DEFAULT
        # executor — §10.a freeze intact (raw routes never queue on the
        # transform pool). ``asyncio.to_thread`` attribute call form is
        # load-bearing: tests spy it via monkeypatch (design §2.2/M4).
        verdict, encoded, coding, etag_value = await asyncio.to_thread(
            _tail_encode, body,
            accept_encoding=accept_encoding, if_none_match=if_none_match,
            rep_version=rep_version,
        )
    else:
        verdict, encoded, coding, etag_value = _tail_encode(
            body, accept_encoding=accept_encoding,
            if_none_match=if_none_match, rep_version=rep_version,
        )
    if verdict is not None:
        return etag_mod.not_modified_response(etag_value, vary)
    headers: dict[str, str] = {
        "Cache-Control": "no-store",
        # §10.a frozen passthrough set (Content-Type/Location/Retry-After/
        # X-Request-ID/Last-Request-ID) — never Content-Encoding.
        **_upstream_passthrough_headers(response),
    }
    headers.update(coding)
    # compress_if_beneficial always emits a bare "Vary: Accept-Encoding";
    # overwrite AFTER merging so directory-sensitive routes keep the
    # merged double value.
    headers["Vary"] = vary
    if etag_value is not None:
        headers["ETag"] = etag_value
    # §10: success status passes through verbatim (201/202/204/206/3xx) —
    # the sidecar never rewrites it and never follows redirects
    # (upstream client follow_redirects=False).
    return Response(encoded, status_code=status, headers=headers)
