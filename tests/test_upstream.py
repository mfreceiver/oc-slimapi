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
        result = self._strip()
        assert "Host" not in result
        assert "Content-Length" not in result
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
