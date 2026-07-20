"""Settings.validate() boundary tests for the bind-host / upstream guards.

These tests pin the loosened bind-host policy introduced when :4097 was
opened as a plaintext direct-entry surface (Tailscale-reachable):

* loopback hosts (``127.0.0.1`` / ``::1`` / ``localhost``) still start — the
  default safe path used by stunnel mTLS in front.
* ``0.0.0.0`` now also starts — the plaintext direct entry. Remote exposure
  is gated by Tailscale ACL / host firewall, not by ``validate()``.
* arbitrary routable hosts (``192.168.x.x`` / public IPs / random hostnames)
  are still rejected so the sidecar cannot accidentally listen on a
  non-deliberate interface.
* the upstream guard is unchanged: **upstream must remain fixed loopback
  HTTP** — opening :4097 to the network does NOT relax the SSRF guard that
  keeps the sidecar from proxying to arbitrary destinations.

Version gate (``X-Slimapi-Version`` middleware) is intentionally NOT covered
here — it lives in ``versioning.py`` and is exercised by every route test.
``Settings.validate()`` does not touch version configuration beyond range
sanity, and that range check is unchanged by the host policy loosening.
"""
from __future__ import annotations

import pytest

from oc_slimapi.config import Settings


def _base(**overrides) -> Settings:
    """Minimal-but-valid Settings; override the field under test per case."""
    base = dict(
        host="127.0.0.1",
        port=4097,
        upstream="http://127.0.0.1:4096",
        route_secret="x" * 32,
        route_secret_file=None,
        server_api_version=1,
        accepted_client_versions=(1, 1),
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Bind host: loopback still accepted (default safe path / stunnel front-end)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_validate_accepts_loopback_host(host):
    """Loopback is the safe default; must always pass validate()."""
    _base(host=host).validate()  # must not raise


# ---------------------------------------------------------------------------
# Bind host: 0.0.0.0 now accepted (plaintext direct entry for Tailscale)
# ---------------------------------------------------------------------------

def test_validate_accepts_wildcard_host():
    """0.0.0.0 is now valid — :4097 is a plaintext direct-entry surface.

    Protecting remote exposure is the operator's responsibility (Tailscale
    ACL / host firewall); validate() no longer hard-rejects the bind.
    """
    _base(host="0.0.0.0").validate()  # must not raise


def test_validate_rejects_routable_host():
    """Arbitrary routable IPs are still rejected — only loopback + 0.0.0.0."""
    settings = _base(host="192.168.1.10")
    with pytest.raises(RuntimeError, match=r"loopback or 0\.0\.0\.0"):
        settings.validate()


def test_validate_rejects_public_host():
    """A public IP is not a deliberate direct-entry surface — reject."""
    settings = _base(host="8.8.8.8")
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_HOST must be"):
        settings.validate()


# ---------------------------------------------------------------------------
# Upstream guard: SSRF defense UNCHANGED by the bind-host loosening.
# ---------------------------------------------------------------------------

def test_validate_still_rejects_non_loopback_upstream():
    """Opening :4097 to the network does NOT relax the upstream SSRF guard.

    The sidecar must still refuse to proxy to a non-loopback upstream, so a
    misconfigured OC_SLIMAPI_UPSTREAM cannot turn the sidecar into an open
    relay now that it can bind 0.0.0.0.
    """
    settings = _base(upstream="http://192.168.1.5:4096")
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_UPSTREAM must be fixed loopback HTTP"):
        settings.validate()


def test_validate_still_rejects_https_upstream():
    """Upstream must stay plain HTTP on loopback (no TLS, no remote scheme)."""
    settings = _base(upstream="https://127.0.0.1:4096")
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_UPSTREAM must be fixed loopback HTTP"):
        settings.validate()


def test_validate_accepts_wildcard_host_with_loopback_upstream():
    """The intended new posture: bind 0.0.0.0, upstream stays loopback."""
    settings = _base(host="0.0.0.0", upstream="http://127.0.0.1:4096")
    settings.validate()  # must not raise


# ---------------------------------------------------------------------------
# Regression: version range sanity check still fires (version GATE itself is
# in versioning.py and is exercised end-to-end by every route test).
# ---------------------------------------------------------------------------

def test_validate_rejects_inverted_version_range():
    """Range sanity in validate() is independent of host policy; still fires."""
    settings = _base(server_api_version=1, accepted_client_versions=(2, 1))
    with pytest.raises(RuntimeError, match=r"slimapi version configuration is invalid"):
        settings.validate()
