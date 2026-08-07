"""Tests for upstream header stripping (M1)."""

from __future__ import annotations

import pytest
from oc_slimapi.upstream import strip_hop_by_hop


class TestStripHopByHop:
    def _make_headers(self, **extras: str) -> dict[str, str]:
        """Minimal headers dict with known hop-by-hop fields."""
        base = {
            "host": "test.com",
            "content-length": "123",
            "connection": "keep-alive, upgrade",
            "keep-alive": "timeout=5",
            "transfer-encoding": "chunked",
            "upgrade": "h2c",
            "content-type": "application/json",
            "x-request-id": "req-abc",
        }
        base.update(**extras)
        return base

    def _strip(self, **extras: str) -> dict[str, str]:
        return strip_hop_by_hop(self._make_headers(**extras))

    def test_keeps_normal_headers(self):
        result = self._strip()
        # Headers are lowercased by httpx request building; check for lowercase
        assert "content-type" in result
        # Host is hop-by-hop and should be stripped
        assert "host" not in result

    def test_removes_hop_by_hop(self):
        """P1-11: hop-by-hop set no longer contains content-length (RFC 7230
        §6.1); it is a representation/framing header and must survive. Real
        hop-by-hop fields (connection/keep-alive/transfer-encoding/upgrade)
        are still stripped."""
        result = self._strip()
        assert "Host" not in result
        # content-length is NOT a hop-by-hop header — keep it. (Key case
        # is whatever the caller supplied; lowercase here.)
        assert "content-length" in result
        assert result["content-length"] == "123"
        assert "Connection" not in result
        assert "Keep-Alive" not in result
        assert "Transfer-Encoding" not in result
        assert "Upgrade" not in result

    def test_keeps_x_request_id(self):
        """Batch1's X-Request-ID must survive."""
        result = self._strip()
        assert "x-request-id" in result
        assert result["x-request-id"] == "req-abc"

    def test_removes_x_forwarded_for(self):
        result = self._strip(**{"x-forwarded-for": "1.2.3.4"})
        assert "x-forwarded-for" not in result

    def test_removes_x_forwarded_host(self):
        result = self._strip(**{"x-forwarded-host": "evil.com"})
        assert "x-forwarded-host" not in result

    def test_removes_x_real_ip(self):
        result = self._strip(**{"x-real-ip": "1.2.3.4"})
        assert "x-real-ip" not in result

    def test_removes_x_real_any(self):
        result = self._strip(**{"x-real-custom": "value"})
        assert "x-real-custom" not in result

    def test_removes_cookie(self):
        result = self._strip(**{"cookie": "session=abc"})
        assert "cookie" not in result

    def test_connection_tokens_respected(self):
        """Connection-token list adds to blocked set."""
        headers = self._make_headers(
            connection="x-custom, keep-alive",
        )
        headers["x-custom"] = "value"
        result = strip_hop_by_hop(headers)
        assert "x-custom" not in result


# ---------------------------------------------------------------------------
# P1-11: content-length is preserved + multi_items preserves duplicate headers
# ---------------------------------------------------------------------------


class TestStripHopByHopP1_11:
    """P1-11 regressions: content-length is no longer stripped, and duplicate
    headers survive via multi_items() (read end), comma-merged in the dict
    output (Starlette Response headers' single-value limitation)."""

    def test_content_length_survives_in_default_headers(self):
        """The default headers dict's content-length=123 must reach the result."""
        headers = {
            "host": "test.com",
            "content-length": "123",
            "connection": "keep-alive",
            "content-type": "application/json",
        }
        result = strip_hop_by_hop(headers)
        assert result["content-length"] == "123"

    def test_proxy_connection_stripped(self):
        """``proxy-connection`` is a non-standard connection-level header
        seen from some intermediaries; it should be stripped alongside the
        RFC connection headers."""
        headers = {"proxy-connection": "keep-alive", "content-type": "x"}
        result = strip_hop_by_hop(headers)
        assert "proxy-connection" not in result

    def test_multi_items_preserves_duplicate_set_cookie(self):
        """Duplicate headers (multiple Set-Cookie) must NOT be silently
        dropped — both values must appear in the comma-merged result. This
        is imperfect for Set-Cookie (its grammar allows commas inside
        values), but losing one of two cookies entirely is strictly worse."""
        # Build a fake headers object exposing multi_items() like
        # httpx.Headers / starlette Headers do.
        class FakeHeaders:
            def multi_items(self):
                return [
                    ("Set-Cookie", "session=abc; Path=/"),
                    ("Set-Cookie", "token=xyz; Path=/"),
                    ("Content-Type", "application/json"),
                ]

        result = strip_hop_by_hop(FakeHeaders())
        # Both Set-Cookie values are present, comma-merged into one slot.
        # (Exact comma-merge format asserted — clients must split on ', ' to
        # recover individual cookies, except where a cookie value itself
        # contains a comma — known imperfect.)
        assert "Set-Cookie" in result
        assert "session=abc; Path=/" in result["Set-Cookie"]
        assert "token=xyz; Path=/" in result["Set-Cookie"]
        assert result["Set-Cookie"] == "session=abc; Path=/, token=xyz; Path=/"

    def test_multi_items_preserves_repeated_cache_control(self):
        """Multiple Cache-Control values are merged into one comma-joined
        value per RFC 7230 §3.2.2."""
        class FakeHeaders:
            def multi_items(self):
                return [
                    ("Cache-Control", "no-store"),
                    ("Cache-Control", "max-age=0"),
                ]

        result = strip_hop_by_hop(FakeHeaders())
        assert result["Cache-Control"] == "no-store, max-age=0"

    def test_multi_items_strips_duplicate_hop_by_hop(self):
        """Even when duplicated, hop-by-hop headers are stripped on each
        occurrence (none survive)."""
        class FakeHeaders:
            def multi_items(self):
                return [
                    ("Connection", "keep-alive"),
                    ("Connection", "upgrade"),  # duplicate hop-by-hop
                    ("X-Custom", "value"),
                ]

        result = strip_hop_by_hop(FakeHeaders())
        assert "Connection" not in result
        assert result["X-Custom"] == "value"

    def test_multi_items_preserves_case_of_first_occurrence(self):
        """The original case of the first occurrence is kept (so a ``Vary``
        header sent as ``Vary`` doesn't become ``vary`` in the forward)."""
        class FakeHeaders:
            def multi_items(self):
                return [
                    ("Vary", "Accept-Encoding"),
                    ("vary", "Accept"),
                ]

        result = strip_hop_by_hop(FakeHeaders())
        # First-occurrence case preserved.
        assert "Vary" in result
        assert "vary" not in result
        assert result["Vary"] == "Accept-Encoding, Accept"

    def test_plain_dict_falls_back_to_items(self):
        """A plain dict (no multi_items) still works — duplicates already
        collapsed by construction, so no information is lost in the
        fallback path."""
        headers = {"content-length": "10", "content-type": "x"}
        result = strip_hop_by_hop(headers)
        assert result == {"content-length": "10", "content-type": "x"}

    def test_content_length_preserved_on_response_path(self):
        """Reframing the catch-all response: an upstream response with
        content-length=42 must keep that header in the StreamingResponse
        forwarded to the client (previously stripped → client couldn't see
        byte count, breaking contract §4 transparent reverse proxy)."""
        # httpx.Headers behaves the same way as in production.
        import httpx
        headers = httpx.Headers([
            ("content-type", "application/json"),
            ("content-length", "42"),
            ("transfer-encoding", "chunked"),  # stripped (real hop-by-hop)
        ])
        result = strip_hop_by_hop(headers)
        assert "content-length" in {k.lower() for k in result}
        assert result["content-length"] == "42"
        assert "transfer-encoding" not in {k.lower() for k in result}
