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


# ---------------------------------------------------------------------------
# Token-stream knobs (NB2 rev-2): Settings.validate() guards the token-stream
# T3 envelope the same way it guards the control-plane envelope (design §6).
# These run here so a future knob tweak cannot silently bypass validation.
# ---------------------------------------------------------------------------

def test_validate_accepts_default_token_stream_knobs():
    """The §6 defaults (8 subs / 64 items / 512KiB / 1MiB) must validate."""
    _base(
        token_stream_max_subscribers=8,
        token_stream_queue_items=64,
        token_stream_buffer_bytes=512 * 1024,
        token_stream_max_frame_bytes=1024 * 1024,
    ).validate()  # must not raise


def test_validate_rejects_zero_token_stream_subscribers():
    settings = _base(token_stream_max_subscribers=0)
    with pytest.raises(RuntimeError, match=r"TOKEN_STREAM_MAX_SUBSCRIBERS must be >= 1"):
        settings.validate()


def test_validate_rejects_single_item_token_stream_queue():
    """Queue < 2 cannot hold both resync + STOP after overflow clear."""
    settings = _base(token_stream_queue_items=1)
    with pytest.raises(RuntimeError, match=r"TOKEN_STREAM_QUEUE_ITEMS must be >= 2"):
        settings.validate()


def test_validate_rejects_zero_token_stream_buffer_bytes():
    settings = _base(token_stream_buffer_bytes=0)
    with pytest.raises(RuntimeError, match=r"TOKEN_STREAM_BUFFER_BYTES must be > 0"):
        settings.validate()


def test_validate_rejects_zero_token_stream_max_frame_bytes():
    settings = _base(token_stream_max_frame_bytes=0)
    with pytest.raises(RuntimeError, match=r"TOKEN_STREAM_MAX_FRAME_BYTES must be > 0"):
        settings.validate()


# ---------------------------------------------------------------------------
# Debug/联调-only budget overrides: default None; validate rejects 0/negative.
# ---------------------------------------------------------------------------

def test_debug_fields_default_none():
    """When unset, the three debug fields must be None (no behaviour change)."""
    s = _base()
    assert s.token_stream_debug_live_budget_bytes is None
    assert s.token_stream_debug_part_max_bytes is None
    assert s.token_stream_debug_live_parts_max is None


def test_validate_rejects_zero_debug_live_budget():
    settings = _base(token_stream_debug_live_budget_bytes=0)
    with pytest.raises(RuntimeError, match=r"DEBUG_LIVE_BUDGET_BYTES must be > 0"):
        settings.validate()


def test_validate_rejects_zero_debug_part_max():
    settings = _base(token_stream_debug_part_max_bytes=0)
    with pytest.raises(RuntimeError, match=r"DEBUG_PART_MAX_BYTES must be > 0"):
        settings.validate()


def test_validate_rejects_zero_debug_live_parts_max():
    settings = _base(token_stream_debug_live_parts_max=0)
    with pytest.raises(RuntimeError, match=r"DEBUG_LIVE_PARTS_MAX must be > 0"):
        settings.validate()


def test_validate_rejects_negative_debug_live_budget():
    settings = _base(token_stream_debug_live_budget_bytes=-1)
    with pytest.raises(RuntimeError, match=r"DEBUG_LIVE_BUDGET_BYTES must be > 0"):
        settings.validate()


def test_validate_rejects_negative_debug_part_max():
    settings = _base(token_stream_debug_part_max_bytes=-1)
    with pytest.raises(RuntimeError, match=r"DEBUG_PART_MAX_BYTES must be > 0"):
        settings.validate()


def test_validate_rejects_negative_debug_live_parts_max():
    settings = _base(token_stream_debug_live_parts_max=-1)
    with pytest.raises(RuntimeError, match=r"DEBUG_LIVE_PARTS_MAX must be > 0"):
        settings.validate()


@pytest.mark.parametrize("val", [1, 42, 1024])
def test_validate_accepts_positive_debug_live_budget(val):
    _base(token_stream_debug_live_budget_bytes=val).validate()  # must not raise


@pytest.mark.parametrize("val", [1, 512, 65536])
def test_validate_accepts_positive_debug_part_max(val):
    _base(token_stream_debug_part_max_bytes=val).validate()  # must not raise


@pytest.mark.parametrize("val", [1, 4, 16])
def test_validate_accepts_positive_debug_live_parts_max(val):
    _base(token_stream_debug_live_parts_max=val).validate()  # must not raise


# ---------------------------------------------------------------------------
# P1-35: port range, byte caps, and server/accepted version consistency.
# ---------------------------------------------------------------------------

def test_validate_rejects_port_zero():
    """Port 0 (OS-picks-random) is not useful for a fixed-port client config."""
    settings = _base(port=0)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_PORT must be in \[1, 65535\]"):
        settings.validate()


def test_validate_rejects_negative_port():
    settings = _base(port=-1)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_PORT must be in \[1, 65535\]"):
        settings.validate()


def test_validate_rejects_port_above_65535():
    settings = _base(port=70000)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_PORT must be in \[1, 65535\]"):
        settings.validate()


def test_validate_accepts_boundary_ports():
    """Port 1 and 65535 are valid boundary values."""
    _base(port=1).validate()       # must not raise
    _base(port=65535).validate()   # must not raise


def test_validate_rejects_oversize_max_response_bytes():
    settings = _base(max_response_bytes=257 * 1024 * 1024)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_MAX_RESPONSE_BYTES must be <= 256 MiB"):
        settings.validate()


def test_validate_rejects_oversize_max_message_bytes():
    settings = _base(max_message_bytes=257 * 1024 * 1024)
    with pytest.raises(RuntimeError, match=r"OC_SLIMAPI_MAX_MESSAGE_BYTES must be <= 256 MiB"):
        settings.validate()


def test_validate_accepts_boundary_byte_caps():
    """Exactly 256 MiB is the boundary — must pass (<= comparison)."""
    _base(max_response_bytes=256 * 1024 * 1024).validate()
    _base(max_message_bytes=256 * 1024 * 1024).validate()


def test_validate_rejects_server_version_outside_accepted_range():
    """server_api_version must be within accepted_client_versions range."""
    settings = _base(server_api_version=5, accepted_client_versions=(1, 3))
    with pytest.raises(RuntimeError, match=r"SERVER_API_VERSION .* must be within .* range"):
        settings.validate()


def test_validate_rejects_server_version_below_accepted_range():
    settings = _base(server_api_version=1, accepted_client_versions=(2, 3))
    with pytest.raises(RuntimeError, match=r"SERVER_API_VERSION .* must be within .* range"):
        settings.validate()


def test_validate_accepts_server_version_at_range_boundaries():
    """server_api_version == minimum and == maximum are both valid."""
    _base(server_api_version=2, accepted_client_versions=(2, 4)).validate()
    _base(server_api_version=4, accepted_client_versions=(2, 4)).validate()


# ---------------------------------------------------------------------------
# P1-40: deployment revision error observability — distinguish unset / not-
# found (silent None) from permission / encoding errors (warning + None).
# Whitespace-only values (env or file) → None.
# ---------------------------------------------------------------------------

def test_deployment_revision_from_env():
    s = _base(deployment_revision="abc123", deployment_revision_file=None)
    assert s.read_deployment_revision() == "abc123"


def test_deployment_revision_env_stripped():
    s = _base(deployment_revision="  abc123  ", deployment_revision_file=None)
    assert s.read_deployment_revision() == "abc123"


def test_deployment_revision_env_whitespace_only_falls_through(tmp_path):
    """Whitespace-only env value should be treated as unset → fall through to file."""
    rev_file = tmp_path / "rev"
    rev_file.write_text("from-file\n")
    s = _base(deployment_revision="   ", deployment_revision_file=str(rev_file))
    assert s.read_deployment_revision() == "from-file"


def test_deployment_revision_unset_and_no_file():
    s = _base(deployment_revision=None, deployment_revision_file=None)
    assert s.read_deployment_revision() is None


def test_deployment_revision_from_file(tmp_path):
    rev_file = tmp_path / "rev"
    rev_file.write_text("  deadbeef  \n")
    s = _base(deployment_revision=None, deployment_revision_file=str(rev_file))
    assert s.read_deployment_revision() == "deadbeef"


def test_deployment_revision_file_not_found_silent(tmp_path):
    """FileNotFoundError → silent None (no warning)."""
    s = _base(deployment_revision=None, deployment_revision_file=str(tmp_path / "nope"))
    assert s.read_deployment_revision() is None


def test_deployment_revision_file_empty_returns_none(tmp_path):
    rev_file = tmp_path / "rev"
    rev_file.write_text("   \n  ")
    s = _base(deployment_revision=None, deployment_revision_file=str(rev_file))
    assert s.read_deployment_revision() is None


def test_deployment_revision_permission_error_warns(tmp_path, caplog):
    """PermissionError → best-effort None but a warning is logged (observability)."""
    rev_file = tmp_path / "rev"
    rev_file.write_text("secret")
    rev_file.chmod(0o000)
    try:
        s = _base(deployment_revision=None, deployment_revision_file=str(rev_file))
        with caplog.at_level("WARNING"):
            result = s.read_deployment_revision()
        assert result is None
        assert any("deployment revision" in r.message for r in caplog.records)
    finally:
        rev_file.chmod(0o644)  # restore so tmp_path cleanup works


def test_deployment_revision_credentials_directory_fallback(tmp_path, monkeypatch):
    creds = tmp_path / "creds"
    creds.mkdir()
    rev_file = creds / "deployment-revision"
    rev_file.write_text("fallback-rev")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    s = _base(deployment_revision=None, deployment_revision_file=None)
    assert s.read_deployment_revision() == "fallback-rev"
