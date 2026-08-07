"""Tests for gzip negotiation and response gating.

Part 1 — unit tests for ``accepts_gzip`` (q-value-correct parsing per RFC 7231
§5.3.4): covers explicit acceptance, explicit refusal via ``gzip;q=0`` (P3-1
fix), case-insensitivity, wildcard, legacy ``x-gzip`` synonym, and malformed q.

Part 2 — regression: ``json_response`` honours ``accept_encoding`` and always
adds ``Content-Encoding: gzip`` when negotiation permits it (contract §9
consistency — every JSON route, including small error bodies, negotiates gzip).

Part 3 — P1-31: ``compress_if_beneficial`` threshold + size-comparison gates,
used by the transform worker pack functions (skeleton / messages / questions)
to avoid gzipping small or incompressible SUCCESS responses where gzip would
make the body larger.
"""

from __future__ import annotations

import gzip as gzip_module

from oc_slimapi.gzip_util import MIN_GZIP_BYTES, accepts_gzip, compress_if_beneficial, json_response


# ===========================================================================
# Part 1 — accepts_gzip
# ===========================================================================

class TestAcceptsGzip:
    """Parametrised via per-method asserts so every case name appears on failure."""

    def test_none(self):
        assert accepts_gzip(None) is False

    def test_empty(self):
        assert accepts_gzip("") is False

    def test_gzip_only(self):
        assert accepts_gzip("gzip") is True

    def test_gzip_q1(self):
        assert accepts_gzip("gzip;q=1") is True

    def test_gzip_q0(self):
        """Explicit ``gzip;q=0`` must be refused (P3-1 correctness fix)."""
        assert accepts_gzip("gzip;q=0") is False

    def test_gzip_case_insensitive(self):
        assert accepts_gzip("GZIP") is True

    def test_br_gzip(self):
        assert accepts_gzip("br,gzip") is True

    def test_gzip_deflate(self):
        assert accepts_gzip("gzip, deflate") is True

    def test_br_only(self):
        assert accepts_gzip("br") is False

    def test_deflate_only(self):
        assert accepts_gzip("deflate") is False

    def test_wildcard(self):
        assert accepts_gzip("*") is True

    def test_wildcard_q0(self):
        assert accepts_gzip("*;q=0") is False

    def test_gzip_q0_overrides_wildcard(self):
        """Explicit ``gzip;q=0`` overrides a later wildcard."""
        assert accepts_gzip("gzip;q=0, *;q=1") is False

    def test_x_gzip_synonym(self):
        """Legacy ``x-gzip`` treated as ``gzip``."""
        assert accepts_gzip("x-gzip") is True

    def test_gzip_q_0_001(self):
        """Very small positive q-value (0.001) must be accepted."""
        assert accepts_gzip("gzip;q=0.001") is True

    def test_gzip_q_malformed(self):
        """Malformed q token falls back to default 1.0 → accepted."""
        assert accepts_gzip("gzip;q=abc") is True


# ===========================================================================
# Part 2 — json_response respects accept_encoding
# ===========================================================================

class TestJsonResponseGzip:
    """``json_response`` must honour ``accept_encoding`` for Content-Encoding.

    Per contract §9, ALL JSON routes (including small error bodies) negotiate
    gzip consistently. ``json_response`` always applies gzip when the client
    accepts it — the size-threshold optimisation (``compress_if_beneficial``)
    is used only by the transform worker pack functions for large SUCCESS
    responses."""

    def test_gzip_q0_no_content_encoding(self):
        """``gzip;q=0`` → no Content-Encoding header."""
        response = json_response({"a": 1}, accept_encoding="gzip;q=0")
        assert "content-encoding" not in response.headers

    def test_gzip_has_content_encoding(self):
        """``gzip`` → Content-Encoding: gzip (contract §9: always honour)."""
        response = json_response({"a": 1}, accept_encoding="gzip")
        assert response.headers.get("content-encoding") == "gzip"


# ===========================================================================
# Part 3 — compress_if_beneficial (P1-31)
# ===========================================================================

class TestCompressIfBeneficial:
    """P1-31: three gates — negotiation, minimum size, actual benefit."""

    def test_small_body_returns_raw(self):
        """Body < MIN_GZIP_BYTES → raw, no Content-Encoding."""
        body = b'{"code":"version_required","accepted":[2,2]}'
        assert len(body) < MIN_GZIP_BYTES
        payload, headers = compress_if_beneficial(body, "gzip")
        assert payload == body
        assert "Content-Encoding" not in headers
        assert headers["Vary"] == "Accept-Encoding"

    def test_large_compressible_body_is_gzipped(self):
        """Body > MIN_GZIP_BYTES + compressible → gzipped."""
        body = (b'{"data":"' + b"x" * 1000 + b'"}')
        payload, headers = compress_if_beneficial(body, "gzip")
        assert len(payload) < len(body)
        assert headers.get("Content-Encoding") == "gzip"

    def test_incompressible_body_returns_raw(self):
        """Body > MIN_GZIP_BYTES but incompressible → raw (compressed >= raw).

        Random bytes don't compress; the size comparison catches this."""
        import os
        body = os.urandom(512)  # well above MIN_GZIP_BYTES, totally random
        payload, headers = compress_if_beneficial(body, "gzip")
        assert payload == body
        assert "Content-Encoding" not in headers

    def test_no_gzip_acceptance_returns_raw(self):
        """Client doesn't accept gzip → raw regardless of size."""
        body = b"x" * 1000
        payload, headers = compress_if_beneficial(body, "identity")
        assert payload == body
        assert "Content-Encoding" not in headers

    def test_none_accept_encoding_returns_raw(self):
        body = b"x" * 1000
        payload, headers = compress_if_beneficial(body, None)
        assert payload == body
        assert "Content-Encoding" not in headers

    def test_already_compressed_body_not_recompressed(self):
        """Defensive: an already-gzipped body is incompressible → not
        re-compressed (gate 3: compressed >= raw → return raw)."""
        raw = b'{"data":"' + b"hello world " * 100 + b'"}'
        already_gzipped = gzip_module.compress(raw, compresslevel=6)
        payload, headers = compress_if_beneficial(already_gzipped, "gzip")
        # Re-compressing a gzipped body produces something >= the input.
        assert payload == already_gzipped
        assert "Content-Encoding" not in headers

    def test_always_sets_vary_header(self):
        """Vary: Accept-Encoding is always present (even without gzip)."""
        _, headers = compress_if_beneficial(b"small", "gzip")
        assert headers["Vary"] == "Accept-Encoding"
        _, headers = compress_if_beneficial(b"x" * 1000, "identity")
        assert headers["Vary"] == "Accept-Encoding"
