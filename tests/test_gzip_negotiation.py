"""Tests for gzip negotiation and response gating.

Part 1 — unit tests for ``accepts_gzip`` (q-value-correct parsing per RFC 7231
§5.3.4): covers explicit acceptance, explicit refusal via ``gzip;q=0`` (P3-1
fix), case-insensitivity, wildcard, legacy ``x-gzip`` synonym, and malformed q.

Part 2 — regression: ``json_response`` honours ``accept_encoding`` and only
adds ``Content-Encoding: gzip`` when negotiation permits it.
"""

from __future__ import annotations

from oc_slimapi.gzip_util import accepts_gzip, json_response


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
    """``json_response`` must honour ``accept_encoding`` for Content-Encoding."""

    def test_gzip_q0_no_content_encoding(self):
        """``gzip;q=0`` → no Content-Encoding header."""
        response = json_response({"a": 1}, accept_encoding="gzip;q=0")
        assert "content-encoding" not in response.headers

    def test_gzip_has_content_encoding(self):
        """``gzip`` → Content-Encoding: gzip."""
        response = json_response({"a": 1}, accept_encoding="gzip")
        assert response.headers.get("content-encoding") == "gzip"
